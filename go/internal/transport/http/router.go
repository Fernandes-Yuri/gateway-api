package http

import (
	"net/http"
	"time"

	"github.com/Fernandes-Yuri/gateway-api/go/internal/repository"
	"github.com/Fernandes-Yuri/gateway-api/go/internal/transport/ws"
	"github.com/go-chi/chi/v5"
	chimw "github.com/go-chi/chi/v5/middleware"
)

// NewRouter configura o roteador Chi com os middlewares de segurança, rotas HTTP e canal WebSocket
func NewRouter(repo repository.FamilyRepository, hub ...*ws.Hub) http.Handler {
	var wsHub *ws.Hub
	if len(hub) > 0 && hub[0] != nil {
		wsHub = hub[0]
	} else {
		wsHub = ws.NewHub()
	}

	r := chi.NewRouter()

	// Middlewares essenciais de observabilidade e proteção global
	r.Use(chimw.RequestID)
	r.Use(chimw.RealIP)
	r.Use(chimw.Logger)
	r.Use(chimw.Recoverer)

	// Rota pública de health check
	healthHandler := func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{
			"status":  "ok",
			"service": "braga-chat",
		})
	}
	r.Get("/health", healthHandler)
	r.Get("/api/health", healthHandler)

	// Canal WebSocket bidirecional em tempo real (SPEC-13 & D47)
	// Registrado sem TimeoutHandler HTTP para permitir conexões persistentes de longa duração
	wsHandler := ws.NewHandler(wsHub, repo)
	r.Get("/ws/family", wsHandler.ServeWS)

	msgHandler := NewMessageHandler(repo, wsHub)
	bindingHandler := NewBindingHandler(repo)
	notificationHandler := NewNotificationHandler(repo)
	// Fase #7: rate limit por IP da criação/revogação de convite familiar
	// (10/min no Python legado). Mantido no processo Go porque o limiter da
	// borda Rust só aplica bloqueio global de IP, não janelas por rota.
	bindingSyncLimiter := rateLimitMiddleware(newSlidingWindowLimiter(10, time.Minute))

	// Grupo de rotas HTTP protegidas com timeout rígido de 10s
	r.Group(func(api chi.Router) {
		api.Use(chimw.Timeout(10 * time.Second))

		// Rotas protegidas por autenticação (X-User-Id da borda Rust ou Bearer fallback)
		api.Group(func(protected chi.Router) {
			protected.Use(AuthMiddleware)

			// Mensagens Familiares E2EE (SPEC-13 & D47)
			//
			// ORDEM CRÍTICA (Etapa 5): registrar TODAS as rotas literais antes das
			// rotas dinâmicas com parâmetro coringa. Se `{patientId}` for avaliado
			// primeiro, um POST /api/family/messages/read casa com o padrão dinâmico
			// (que só aceita GET) e o Chi responde 405 Method Not Allowed (Allow: GET).
			// Literais primeiro:
			protected.Post("/api/family/messages/read", msgHandler.HandleMarkMessagesRead)
			protected.Post("/api/family/message/delete", msgHandler.HandleDeleteMessage)
			protected.Post("/api/family/messages/send", msgHandler.HandleSendMessage)
			protected.Post("/api/sync/family-message", msgHandler.HandleSendMessage)
			// Fase #7: vínculo familiar local->nuvem (antes no Python).
			protected.With(bindingSyncLimiter).Post("/api/sync/family-binding", bindingHandler.HandleSyncBinding)
			protected.Get("/api/family/messages", msgHandler.HandleGetMessages)
			// Dinâmicas depois:
			protected.Get("/api/family/messages/{patientId}", msgHandler.HandleGetMessages)

			// Vínculos Familiares & ECDH
			protected.Get("/api/family/find-by-code/{code}", bindingHandler.HandleFindByCode)
			protected.Post("/api/family/accept", bindingHandler.HandleAcceptInvitation)
			protected.Post("/api/family/e2ee/public-key", bindingHandler.HandlePublishPublicKey)
			protected.Get("/api/family/bindings/{userId}", bindingHandler.HandleListBindings)

			// Central de Notificações persistente por destinatário (RTL-07)
			protected.Get("/api/notifications", notificationHandler.HandleListNotifications)
			protected.Get("/api/notifications/changes", notificationHandler.HandleNotificationChanges)
			protected.Get("/api/notifications/unread", notificationHandler.HandleNotificationUnread)
			protected.Get("/api/notifications/unread-count", notificationHandler.HandleNotificationUnread)
			protected.Post("/api/notifications/read", notificationHandler.HandleMarkNotifications)
			protected.Post("/api/notifications/mark-read", notificationHandler.HandleMarkNotifications)
			protected.Post("/api/notifications/mark-all-read", notificationHandler.HandleMarkAllNotificationsRead)
			protected.Post("/api/notifications/archive", notificationHandler.HandleArchiveNotifications)
		})
	})

	return r
}

package http

import (
	"net/http"
	"time"

	"github.com/Fernandes-Yuri/gateway-api/go/internal/repository"
	"github.com/go-chi/chi/v5"
	chimw "github.com/go-chi/chi/v5/middleware"
)

// NewRouter configura o roteador Chi com os middlewares de segurança e todos os endpoints do braga-chat
func NewRouter(repo repository.FamilyRepository) http.Handler {
	r := chi.NewRouter()

	// Middlewares essenciais de observabilidade e proteção
	r.Use(chimw.RequestID)
	r.Use(chimw.RealIP)
	r.Use(chimw.Logger)
	r.Use(chimw.Recoverer)
	r.Use(chimw.Timeout(10 * time.Second))

	// Rota pública de health check
	healthHandler := func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{
			"status":  "ok",
			"service": "braga-chat",
		})
	}
	r.Get("/health", healthHandler)
	r.Get("/api/health", healthHandler)

	msgHandler := NewMessageHandler(repo)
	bindingHandler := NewBindingHandler(repo)

	// Rotas protegidas por autenticação (X-User-Id da borda Rust ou Bearer fallback)
	r.Group(func(protected chi.Router) {
		protected.Use(AuthMiddleware)

		// Mensagens Familiares E2EE (SPEC-13 & D47)
		protected.Post("/api/family/messages/send", msgHandler.HandleSendMessage)
		protected.Post("/api/sync/family-message", msgHandler.HandleSendMessage)
		protected.Get("/api/family/messages/{patientId}", msgHandler.HandleGetMessages)
		protected.Get("/api/family/messages", msgHandler.HandleGetMessages)
		protected.Post("/api/family/message/delete", msgHandler.HandleDeleteMessage)

		// Vínculos Familiares & ECDH
		protected.Get("/api/family/find-by-code/{code}", bindingHandler.HandleFindByCode)
		protected.Post("/api/family/accept", bindingHandler.HandleAcceptInvitation)
		protected.Post("/api/family/e2ee/public-key", bindingHandler.HandlePublishPublicKey)
		protected.Get("/api/family/bindings/{userId}", bindingHandler.HandleListBindings)
	})

	return r
}

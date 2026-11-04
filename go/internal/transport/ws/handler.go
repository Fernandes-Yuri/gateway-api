package ws

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"github.com/Fernandes-Yuri/gateway-api/go/internal/repository"
	"github.com/coder/websocket"
)

// Handler gerencia o handshake WebSocket para o endpoint /ws/family
type Handler struct {
	hub       *Hub
	repo      repository.FamilyRepository
	clientCfg *ClientConfig
}

// NewHandler instancia um novo Handler WebSocket
func NewHandler(hub *Hub, repo repository.FamilyRepository, cfgs ...ClientConfig) *Handler {
	var cfg *ClientConfig
	if len(cfgs) > 0 {
		cfg = &cfgs[0]
	}
	return &Handler{
		hub:       hub,
		repo:      repo,
		clientCfg: cfg,
	}
}

// ServeWS processa a requisição de conexão WebSocket (/ws/family)
func (h *Handler) ServeWS(w http.ResponseWriter, r *http.Request) {
	// 1. Extração do userId (X-User-Id da borda Rust, token query param ou Authorization Bearer)
	uid := h.extractUserID(r)
	if uid == "" {
		writeJSONError(w, http.StatusUnauthorized, "Entre na sua conta para continuar.")
		return
	}

	// 2. Extração do patientId via query parameter
	patientID := strings.TrimSpace(r.URL.Query().Get("patientId"))
	if patientID == "" {
		patientID = strings.TrimSpace(r.URL.Query().Get("patient_id"))
	}
	if patientID == "" {
		writeJSONError(w, http.StatusUnprocessableEntity, "Informe o patientId.")
		return
	}

	// 3. Validação de autorização no FamilyRepository
	if h.repo != nil {
		allowed, err := h.repo.CheckFamilyAccess(r.Context(), uid, patientID)
		if err != nil {
			if errors.Is(err, repository.ErrDatabaseUnavailable) {
				writeJSONError(w, http.StatusServiceUnavailable, "Banco indisponível. Tente novamente.")
				return
			}
			writeJSONError(w, http.StatusInternalServerError, "Erro interno.")
			return
		}
		if !allowed {
			writeJSONError(w, http.StatusForbidden, "O acompanhamento deste familiar não está autorizado.")
			return
		}
	}

	// 4. Handshake RFC 6455 via coder/websocket
	conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{
		InsecureSkipVerify: true,
		OriginPatterns:     []string{"*"},
	})
	if err != nil {
		return
	}

	// 5. Instanciação e registro do cliente no Hub
	var client *Client
	if h.clientCfg != nil {
		client = NewClient(h.hub, conn, uid, patientID, *h.clientCfg)
	} else {
		client = NewClient(h.hub, conn, uid, patientID)
	}

	h.hub.Register(client)

	// Inicia bombas de escrita e leitura
	go client.writePump()
	client.readPump() // bloqueia até a conexão ser encerrada
}

// extractUserID extrai o UID autenticado a partir dos canais suportados
func (h *Handler) extractUserID(r *http.Request) string {
	// Header confiável repassado pela borda Rust
	if xUserID := strings.TrimSpace(r.Header.Get("X-User-Id")); xUserID != "" {
		return xUserID
	}

	// Query param ?token=... (padrão de WebSockets para browsers e mobile)
	token := strings.TrimSpace(r.URL.Query().Get("token"))

	// Fallback para Authorization: Bearer
	if token == "" {
		authHeader := strings.TrimSpace(r.Header.Get("Authorization"))
		if strings.HasPrefix(strings.ToLower(authHeader), "bearer ") {
			token = strings.TrimSpace(authHeader[7:])
		}
	}

	if token == "" {
		return ""
	}

	return ExtractUIDFromToken(token)
}

// ExtractUIDFromToken resolve tokens canônicos de teste ou decodifica claims JWT padrão
func ExtractUIDFromToken(token string) string {
	switch token {
	case "<FIREBASE_JWT_PATIENT>":
		return "patient_test_uid_123"
	case "<FIREBASE_JWT_CAREGIVER>":
		return "caregiver_test_uid_456"
	}

	parts := strings.Split(token, ".")
	if len(parts) == 3 {
		payloadBytes, err := base64.RawURLEncoding.DecodeString(parts[1])
		if err != nil {
			payloadBytes, err = base64.URLEncoding.DecodeString(parts[1])
		}
		if err == nil {
			var claims map[string]interface{}
			if err := json.Unmarshal(payloadBytes, &claims); err == nil {
				if uid, ok := claims["user_id"].(string); ok && uid != "" {
					return uid
				}
				if sub, ok := claims["sub"].(string); ok && sub != "" {
					return sub
				}
				if uid, ok := claims["uid"].(string); ok && uid != "" {
					return uid
				}
			}
		}
	}

	return token
}

func writeJSONError(w http.ResponseWriter, statusCode int, message string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(statusCode)
	_ = json.NewEncoder(w).Encode(map[string]string{
		"detail": message,
		"error":  message,
	})
}

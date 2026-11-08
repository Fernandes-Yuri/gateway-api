package http

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/Fernandes-Yuri/gateway-api/go/internal/domain"
	"github.com/Fernandes-Yuri/gateway-api/go/internal/repository"
	"github.com/Fernandes-Yuri/gateway-api/go/internal/transport/ws"
	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
)

// MessageHandler gerencia os endpoints de mensagens familiares E2EE
type MessageHandler struct {
	repo repository.FamilyRepository
	hub  *ws.Hub
}

// NewMessageHandler instancia o handler de mensagens
func NewMessageHandler(repo repository.FamilyRepository, hub ...*ws.Hub) *MessageHandler {
	var h *ws.Hub
	if len(hub) > 0 {
		h = hub[0]
	}
	return &MessageHandler{repo: repo, hub: h}
}

// HandleSendMessage processa o envio de mensagens respeitando Blind Relay e TTL de 24h
func (h *MessageHandler) HandleSendMessage(w http.ResponseWriter, r *http.Request) {
	uid, ok := GetUserID(r.Context())
	if !ok || uid == "" {
		writeJSONError(w, http.StatusUnauthorized, "Entre na sua conta para continuar.")
		return
	}

	var req domain.SendFamilyMessageRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSONError(w, http.StatusUnprocessableEntity, "Payload JSON inválido.")
		return
	}

	// Valida se o emissor é o proprietário do token
	if uid != req.SenderID {
		writeJSONError(w, http.StatusForbidden, "Esta operação pertence a outra conta.")
		return
	}

	// Valida formato do UUID da mensagem
	if _, err := uuid.Parse(req.ID); err != nil {
		writeJSONError(w, http.StatusUnprocessableEntity, "Mensagem inválida.")
		return
	}

	// Valida expiração e relógio
	sent, expires, err := domain.ParseMessageLifetime(req.SentAt, time.Time{})
	if err != nil {
		if errors.Is(err, domain.ErrFutureSentAt) {
			writeJSONError(w, http.StatusUnprocessableEntity, "Confira a data e a hora do aparelho.")
			return
		}
		if errors.Is(err, domain.ErrExpiredMessage) {
			writeJSONError(w, http.StatusGone, "Mensagem expirada.")
			return
		}
		writeJSONError(w, http.StatusUnprocessableEntity, "Horário de envio inválido.")
		return
	}

	// Validação da criptografia ponta a ponta (SPEC-13 §2.2)
	if req.EncryptionVersion != nil && *req.EncryptionVersion != 0 && *req.EncryptionVersion != domain.E2EECurrentVersion {
		writeJSONError(w, http.StatusUnprocessableEntity, "Versão de criptografia E2EE não suportada.")
		return
	}

	var encryptedBlob *string
	var encVer int16 = 0
	var dbMessageText string

	if req.BodyEncrypted != nil || (req.EncryptionVersion != nil && *req.EncryptionVersion == domain.E2EECurrentVersion) {
		candidate := ""
		if req.BodyEncrypted != nil {
			candidate = *req.BodyEncrypted
		} else {
			candidate = req.MessageText
		}

		parsed, err := domain.ParseE2EEEnvelope(candidate, true)
		if err != nil {
			writeJSONError(w, http.StatusUnprocessableEntity, err.Error())
			return
		}
		encryptedBlob = &parsed
		encVer = domain.E2EECurrentVersion
		dbMessageText = ""
	} else {
		autoEnvelope, _ := domain.ParseE2EEEnvelope(req.MessageText, false)
		if autoEnvelope != "" {
			encryptedBlob = &autoEnvelope
			encVer = domain.E2EECurrentVersion
			dbMessageText = ""
		} else {
			encVer = 0
			dbMessageText = req.MessageText
		}
	}

	// Verifica permissão de acesso ao titular
	allowed, err := h.repo.CheckFamilyAccess(r.Context(), uid, req.PatientID)
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

	msg := domain.FamilyMessage{
		ID:                req.ID,
		PatientID:         req.PatientID,
		SenderID:          uid,
		SenderName:        strings.TrimSpace(req.SenderName),
		MessageText:       dbMessageText,
		BodyEncrypted:     encryptedBlob,
		EncryptionVersion: encVer,
		IconType:          req.IconType,
		IsRead:            false,
		SentAt:            sent,
		ExpiresAt:         expires,
	}

	if err := h.repo.SaveMessage(r.Context(), &msg); err != nil {
		if errors.Is(err, repository.ErrForbidden) {
			writeJSONError(w, http.StatusForbidden, "O acompanhamento deste familiar não está autorizado.")
			return
		}
		if errors.Is(err, repository.ErrConflict) {
			writeJSONError(w, http.StatusConflict, "Mensagem indisponível.")
			return
		}
		if errors.Is(err, repository.ErrDatabaseUnavailable) {
			writeJSONError(w, http.StatusServiceUnavailable, "Banco indisponível. Tente novamente.")
			return
		}
		writeJSONError(w, http.StatusInternalServerError, "Falha ao gravar mensagem.")
		return
	}

	// Notificação WebSocket em tempo real para familiares conectados (< 2ms)
	if h.hub != nil {
		_ = h.hub.BroadcastFamilyMessage(msg.PatientID, &msg)
	}

	writeJSON(w, http.StatusOK, domain.SendFamilyMessageResponse{
		Status:            "success",
		ID:                req.ID,
		EncryptionVersion: encVer,
	})
}

// HandleGetMessages retorna a lista de mensagens ativas e tombstones não expirados
func (h *MessageHandler) HandleGetMessages(w http.ResponseWriter, r *http.Request) {
	uid, ok := GetUserID(r.Context())
	if !ok || uid == "" {
		writeJSONError(w, http.StatusUnauthorized, "Entre na sua conta para continuar.")
		return
	}

	patientID := chi.URLParam(r, "patientId")
	if patientID == "" {
		patientID = r.URL.Query().Get("patient_id")
		if patientID == "" {
			patientID = r.URL.Query().Get("patientId")
		}
	}
	if patientID == "" {
		writeJSONError(w, http.StatusUnprocessableEntity, "Informe o patientId.")
		return
	}

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

	limit := 50
	if l := r.URL.Query().Get("limit"); l != "" {
		if parsedL, err := strconv.Atoi(l); err == nil && parsedL > 0 {
			limit = parsedL
			if limit > 200 {
				limit = 200
			}
		}
	}

	offset := 0
	if o := r.URL.Query().Get("offset"); o != "" {
		if parsedO, err := strconv.Atoi(o); err == nil && parsedO >= 0 {
			offset = parsedO
		}
	}

	messages, err := h.repo.GetMessages(r.Context(), patientID, limit, offset)
	if err != nil {
		if errors.Is(err, repository.ErrDatabaseUnavailable) {
			writeJSONError(w, http.StatusServiceUnavailable, "Banco indisponível. Tente novamente.")
			return
		}
		writeJSONError(w, http.StatusInternalServerError, "Falha ao obter mensagens.")
		return
	}

	writeJSON(w, http.StatusOK, messages)
}

// HandleDeleteMessage aplica tombstone à mensagem pelo remetente original
func (h *MessageHandler) HandleDeleteMessage(w http.ResponseWriter, r *http.Request) {
	uid, ok := GetUserID(r.Context())
	if !ok || uid == "" {
		writeJSONError(w, http.StatusUnauthorized, "Entre na sua conta para continuar.")
		return
	}

	var req domain.DeleteFamilyMessageRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSONError(w, http.StatusUnprocessableEntity, "Payload JSON inválido.")
		return
	}

	if _, err := uuid.Parse(req.ID); err != nil {
		writeJSONError(w, http.StatusUnprocessableEntity, "Mensagem inválida.")
		return
	}

	var sentAtTime *time.Time
	if req.SentAt != nil && *req.SentAt != "" {
		sent, _, err := domain.ParseMessageLifetime(*req.SentAt, time.Time{})
		if err == nil {
			sentAtTime = &sent
		}
	}

	err := h.repo.DeleteMessage(r.Context(), req.ID, uid, req.PatientID, sentAtTime)
	if err != nil {
		if errors.Is(err, repository.ErrForbidden) {
			writeJSONError(w, http.StatusForbidden, "Só quem enviou pode apagar a mensagem.")
			return
		}
		if errors.Is(err, repository.ErrDatabaseUnavailable) {
			writeJSONError(w, http.StatusServiceUnavailable, "Banco indisponível. Tente novamente.")
			return
		}
		writeJSONError(w, http.StatusInternalServerError, "Falha ao excluir mensagem.")
		return
	}

	// Notificação WebSocket em tempo real sobre tombstone da mensagem
	if h.hub != nil {
		patientID := ""
		if req.PatientID != nil {
			patientID = *req.PatientID
		}
		if patientID != "" {
			_ = h.hub.BroadcastMessageDeleted(patientID, req.ID)
		}
	}

	writeJSON(w, http.StatusOK, map[string]string{
		"status": "success",
		"id":     req.ID,
	})
}

// HandleMarkMessagesRead marca mensagens de uma conversa familiar como lidas
func (h *MessageHandler) HandleMarkMessagesRead(w http.ResponseWriter, r *http.Request) {
	uid, ok := GetUserID(r.Context())
	if !ok || uid == "" {
		writeJSONError(w, http.StatusUnauthorized, "Entre na sua conta para continuar.")
		return
	}

	var req domain.MarkMessagesReadRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSONError(w, http.StatusUnprocessableEntity, "Payload JSON inválido.")
		return
	}

	patientID := strings.TrimSpace(req.PatientID)
	if patientID == "" {
		writeJSONError(w, http.StatusUnprocessableEntity, "patientId obrigatório.")
		return
	}

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
		writeJSONError(w, http.StatusForbidden, "Acesso não autorizado.")
		return
	}

	if len(req.MessageIDs) > 100 {
		writeJSONError(w, http.StatusUnprocessableEntity, "Informe até 100 mensagens por recibo.")
		return
	}
	if req.MessageIDs != nil && len(req.MessageIDs) == 0 {
		writeJSON(w, http.StatusOK, map[string]string{"status": "success"})
		return
	}
	if err := h.repo.MarkMessagesAsRead(r.Context(), patientID, uid, req.MessageIDs); err != nil {
		if errors.Is(err, repository.ErrForbidden) {
			writeJSONError(w, http.StatusForbidden, "Acesso não autorizado.")
			return
		}
		if errors.Is(err, repository.ErrDatabaseUnavailable) {
			writeJSONError(w, http.StatusServiceUnavailable, "Banco indisponível. Tente novamente.")
			return
		}
		writeJSONError(w, http.StatusInternalServerError, "Falha ao marcar como lidas.")
		return
	}

	if h.hub != nil {
		_ = h.hub.BroadcastMessagesRead(patientID, uid, req.MessageIDs)
	}

	writeJSON(w, http.StatusOK, map[string]string{"status": "success"})
}

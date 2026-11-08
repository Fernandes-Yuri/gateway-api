package http

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"

	"github.com/Fernandes-Yuri/gateway-api/go/internal/repository"
)

// NotificationHandler expõe a central de notificações autenticada por destinatário.
// Leitura ou arquivamento na central nunca emitem recibo de leitura de chat.
type NotificationHandler struct {
	repo repository.NotificationRepository
}

func NewNotificationHandler(repo repository.FamilyRepository) *NotificationHandler {
	notifRepo, _ := repo.(repository.NotificationRepository)
	return &NotificationHandler{repo: notifRepo}
}

type markNotificationsRequest struct {
	ItemID          *string `json:"itemId"`
	ThroughRevision *int64  `json:"throughRevision"`
	Archive         *bool   `json:"archive"`
}

func (h *NotificationHandler) requireAuthAndRepo(w http.ResponseWriter, r *http.Request) (string, bool) {
	uid, ok := GetUserID(r.Context())
	if !ok || strings.TrimSpace(uid) == "" {
		writeJSONError(w, http.StatusUnauthorized, "Entre na sua conta para continuar.")
		return "", false
	}
	if h == nil || h.repo == nil {
		writeJSONError(w, http.StatusServiceUnavailable, "Central de notificações indisponível.")
		return "", false
	}
	return uid, true
}

func writeNotificationError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, repository.ErrNotificationValidation):
		writeJSONError(w, http.StatusUnprocessableEntity, "Parâmetros da central de notificações inválidos.")
	case errors.Is(err, repository.ErrForbidden):
		writeJSONError(w, http.StatusForbidden, "Esta operação pertence a outra conta.")
	case errors.Is(err, repository.ErrNotFound):
		writeJSONError(w, http.StatusNotFound, "Notificação não encontrada.")
	case errors.Is(err, repository.ErrDatabaseUnavailable):
		writeJSONError(w, http.StatusServiceUnavailable, "Banco indisponível. Tente novamente.")
	default:
		writeJSONError(w, http.StatusInternalServerError, "Falha ao consultar central de notificações.")
	}
}

// HandleListNotifications retorna página paginada por cursor estrito da central.
func (h *NotificationHandler) HandleListNotifications(w http.ResponseWriter, r *http.Request) {
	uid, ok := h.requireAuthAndRepo(w, r)
	if !ok {
		return
	}

	q := r.URL.Query()
	limit := 20
	if rawLimit := strings.TrimSpace(q.Get("limit")); rawLimit != "" {
		parsed, err := strconv.Atoi(rawLimit)
		if err != nil {
			writeNotificationError(w, repository.ErrNotificationValidation)
			return
		}
		limit = parsed
	}

	includeArchived := false
	if rawArchived := strings.TrimSpace(q.Get("includeArchived")); rawArchived != "" {
		parsed, err := strconv.ParseBool(rawArchived)
		if err != nil {
			writeNotificationError(w, repository.ErrNotificationValidation)
			return
		}
		includeArchived = parsed
	}

	opts := repository.NotificationListOptions{
		Cursor:          strings.TrimSpace(q.Get("cursor")),
		Limit:           limit,
		Category:        strings.ToUpper(strings.TrimSpace(q.Get("category"))),
		IncludeArchived: includeArchived,
	}

	page, err := h.repo.ListNotifications(r.Context(), uid, opts)
	if err != nil {
		writeNotificationError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, page)
}

// HandleNotificationChanges retorna delta incremental por revisão.
func (h *NotificationHandler) HandleNotificationChanges(w http.ResponseWriter, r *http.Request) {
	uid, ok := h.requireAuthAndRepo(w, r)
	if !ok {
		return
	}

	q := r.URL.Query()
	var after int64
	if rawAfter := strings.TrimSpace(q.Get("after")); rawAfter != "" {
		parsed, err := strconv.ParseInt(rawAfter, 10, 64)
		if err != nil {
			writeNotificationError(w, repository.ErrNotificationValidation)
			return
		}
		after = parsed
	}

	limit := 50
	if rawLimit := strings.TrimSpace(q.Get("limit")); rawLimit != "" {
		parsed, err := strconv.Atoi(rawLimit)
		if err != nil {
			writeNotificationError(w, repository.ErrNotificationValidation)
			return
		}
		limit = parsed
	}

	delta, err := h.repo.NotificationChanges(r.Context(), uid, after, limit)
	if err != nil {
		writeNotificationError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, delta)
}

// HandleNotificationUnread retorna a revisão atual e o total de grupos não lidos.
func (h *NotificationHandler) HandleNotificationUnread(w http.ResponseWriter, r *http.Request) {
	uid, ok := h.requireAuthAndRepo(w, r)
	if !ok {
		return
	}

	state, err := h.repo.NotificationUnread(r.Context(), uid)
	if err != nil {
		writeNotificationError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, state)
}

func decodeMarkRequest(r *http.Request) (*markNotificationsRequest, error) {
	var req markNotificationsRequest
	dec := json.NewDecoder(io.LimitReader(r.Body, 16384))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		return nil, repository.ErrNotificationValidation
	}
	var trailing any
	if dec.Decode(&trailing) != io.EOF {
		return nil, repository.ErrNotificationValidation
	}
	if req.ThroughRevision == nil {
		return nil, repository.ErrNotificationValidation
	}
	return &req, nil
}

// HandleMarkNotifications marca um item específico ou todos até throughRevision como lidos/arquivados.
func (h *NotificationHandler) HandleMarkNotifications(w http.ResponseWriter, r *http.Request) {
	h.handleMark(w, r, false, false)
}

// HandleMarkAllNotificationsRead marca todos os itens até throughRevision como lidos.
func (h *NotificationHandler) HandleMarkAllNotificationsRead(w http.ResponseWriter, r *http.Request) {
	h.handleMark(w, r, true, false)
}

// HandleArchiveNotifications arquiva um item específico ou todos até throughRevision.
func (h *NotificationHandler) HandleArchiveNotifications(w http.ResponseWriter, r *http.Request) {
	h.handleMark(w, r, false, true)
}

func (h *NotificationHandler) handleMark(w http.ResponseWriter, r *http.Request, forceAll, forceArchive bool) {
	uid, ok := h.requireAuthAndRepo(w, r)
	if !ok {
		return
	}

	req, err := decodeMarkRequest(r)
	if err != nil {
		writeNotificationError(w, err)
		return
	}

	itemID := ""
	if req.ItemID != nil {
		itemID = strings.TrimSpace(*req.ItemID)
		if itemID == "" {
			writeNotificationError(w, repository.ErrNotificationValidation)
			return
		}
	}
	if forceAll && itemID != "" {
		writeNotificationError(w, repository.ErrNotificationValidation)
		return
	}

	archive := forceArchive
	if req.Archive != nil {
		if forceArchive && !*req.Archive {
			writeNotificationError(w, repository.ErrNotificationValidation)
			return
		}
		archive = *req.Archive
	}

	state, err := h.repo.MarkNotifications(r.Context(), uid, itemID, *req.ThroughRevision, archive)
	if err != nil {
		writeNotificationError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, state)
}

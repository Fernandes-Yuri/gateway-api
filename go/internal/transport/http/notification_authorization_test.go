package http_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Fernandes-Yuri/gateway-api/go/internal/domain"
	"github.com/Fernandes-Yuri/gateway-api/go/internal/repository"
	transporthttp "github.com/Fernandes-Yuri/gateway-api/go/internal/transport/http"
	"github.com/google/uuid"
)

type revokedDuringMessageWriteRepository struct{ *MockFamilyRepository }

func (*revokedDuringMessageWriteRepository) SaveMessage(context.Context, *domain.FamilyMessage) error {
	return repository.ErrForbidden
}

func (*revokedDuringMessageWriteRepository) MarkMessagesAsRead(context.Context, string, string, ...[]string) error {
	return repository.ErrForbidden
}

func TestMessageRevocationBetweenPrecheckAndWriteReturnsForbidden(t *testing.T) {
	repo := &revokedDuringMessageWriteRepository{NewMockFamilyRepository()}
	repo.AllowAccess("caregiver", "patient")
	router := transporthttp.NewRouter(repo)
	for _, request := range []struct{ path, body string }{
		{"/api/family/messages/send", `{"id":"` + uuid.NewString() + `","patientId":"patient","senderId":"caregiver","sentAt":"` +
			time.Now().UTC().Format(time.RFC3339) + `","messageText":"Fixture"}`},
		{"/api/family/messages/read", `{"patientId":"patient","messageIds":["` + uuid.NewString() + `"]}`},
	} {
		req := httptest.NewRequest(http.MethodPost, request.path, strings.NewReader(request.body))
		req.Header.Set("X-User-Id", "caregiver")
		response := httptest.NewRecorder()
		router.ServeHTTP(response, req)
		if response.Code != http.StatusForbidden {
			t.Fatalf("%s: esperado 403, recebido %d", request.path, response.Code)
		}
	}
}

type fakeNotificationRepo struct {
	*MockFamilyRepository
	lastUID     string
	lastItemID  string
	lastThrough int64
	lastArchive bool
	markErr     error
}

func (f *fakeNotificationRepo) ListNotifications(_ context.Context, uid string, opts repository.NotificationListOptions) (*repository.NotificationPage, error) {
	if opts.Limit < 1 || opts.Limit > 100 || (opts.Category != "" && opts.Category != "FAMILY" && opts.Category != "AGENDA" && opts.Category != "COMMUNITY" && opts.Category != "CARE") {
		return nil, repository.ErrNotificationValidation
	}
	f.lastUID = uid
	return &repository.NotificationPage{
		Contract:         repository.NotificationContractVersion,
		Items:            []json.RawMessage{json.RawMessage(`{"id":"item-1","kind":"FAMILY_MESSAGE"}`)},
		SnapshotRevision: 7,
		LatestRevision:   7,
		UnreadCount:      2,
	}, nil
}

func (f *fakeNotificationRepo) NotificationChanges(_ context.Context, uid string, after int64, limit int) (*repository.NotificationDelta, error) {
	if after < 0 || limit < 1 || limit > 200 {
		return nil, repository.ErrNotificationValidation
	}
	f.lastUID = uid
	return &repository.NotificationDelta{
		Contract:       repository.NotificationContractVersion,
		Changes:        []repository.NotificationChange{},
		NextCursor:     after,
		LatestRevision: 7,
		UnreadCount:    2,
	}, nil
}

func (f *fakeNotificationRepo) NotificationUnread(_ context.Context, uid string) (*repository.NotificationState, error) {
	f.lastUID = uid
	return &repository.NotificationState{
		Contract:       repository.NotificationContractVersion,
		LatestRevision: 7,
		UnreadCount:    2,
	}, nil
}

func (f *fakeNotificationRepo) MarkNotifications(_ context.Context, uid, itemID string, through int64, archive bool) (*repository.NotificationState, error) {
	if f.markErr != nil {
		return nil, f.markErr
	}
	if through < 0 {
		return nil, repository.ErrNotificationValidation
	}
	f.lastUID = uid
	f.lastItemID = itemID
	f.lastThrough = through
	f.lastArchive = archive
	return &repository.NotificationState{
		Contract:       repository.NotificationContractVersion,
		LatestRevision: 8,
		UnreadCount:    0,
	}, nil
}

func TestNotificationCenterRoutesEnforceAuthenticatedUIDAndStrictValidation(t *testing.T) {
	repo := &fakeNotificationRepo{MockFamilyRepository: NewMockFamilyRepository()}
	router := transporthttp.NewRouter(repo)

	// 1. Sem autenticação -> 401
	unauth := httptest.NewRequest(http.MethodGet, "/api/notifications", nil)
	res := httptest.NewRecorder()
	router.ServeHTTP(res, unauth)
	if res.Code != http.StatusUnauthorized {
		t.Fatalf("sem auth: esperado 401, recebido %d", res.Code)
	}

	// 2. Listagem válida usa o UID autenticado
	listReq := httptest.NewRequest(http.MethodGet, "/api/notifications?limit=10&category=family", nil)
	listReq.Header.Set("X-User-Id", "user-auth-1")
	res = httptest.NewRecorder()
	router.ServeHTTP(res, listReq)
	if res.Code != http.StatusOK || repo.lastUID != "user-auth-1" {
		t.Fatalf("listagem: status=%d uid=%s", res.Code, repo.lastUID)
	}

	// 3. Parâmetros inválidos ou tentativa de injetar recipientId no body -> 422
	for _, tc := range []struct {
		method string
		path   string
		body   string
	}{
		{http.MethodGet, "/api/notifications?limit=0", ""},
		{http.MethodGet, "/api/notifications?category=INVALID", ""},
		{http.MethodGet, "/api/notifications?includeArchived=talvez", ""},
		{http.MethodGet, "/api/notifications/changes?after=-1", ""},
		{http.MethodPost, "/api/notifications/read", `{}`},
		{http.MethodPost, "/api/notifications/read", `{"throughRevision":5,"recipientId":"victim"}`},
		{http.MethodPost, "/api/notifications/mark-all-read", `{"itemId":"` + uuid.NewString() + `","throughRevision":5}`},
		{http.MethodPost, "/api/notifications/archive", `{"throughRevision":5,"archive":false}`},
	} {
		req := httptest.NewRequest(tc.method, tc.path, strings.NewReader(tc.body))
		req.Header.Set("X-User-Id", "user-auth-1")
		rec := httptest.NewRecorder()
		router.ServeHTTP(rec, req)
		if rec.Code != http.StatusUnprocessableEntity {
			t.Fatalf("%s %s (%s): esperado 422, recebido %d", tc.method, tc.path, tc.body, rec.Code)
		}
	}

	// 4. Marcar como lida e arquivar usam throughRevision e UID autenticado
	itemID := uuid.NewString()
	markReq := httptest.NewRequest(http.MethodPost, "/api/notifications/archive",
		strings.NewReader(`{"itemId":"`+itemID+`","throughRevision":7}`))
	markReq.Header.Set("X-User-Id", "user-auth-2")
	res = httptest.NewRecorder()
	router.ServeHTTP(res, markReq)
	if res.Code != http.StatusOK || repo.lastUID != "user-auth-2" || repo.lastItemID != itemID || repo.lastThrough != 7 || !repo.lastArchive {
		t.Fatalf("archive inesperado: status=%d uid=%s item=%s through=%d archive=%v",
			res.Code, repo.lastUID, repo.lastItemID, repo.lastThrough, repo.lastArchive)
	}

	// 5. Item inexistente ou de terceiro -> 404
	repo.markErr = repository.ErrNotFound
	notFoundReq := httptest.NewRequest(http.MethodPost, "/api/notifications/read",
		strings.NewReader(`{"itemId":"`+itemID+`","throughRevision":7}`))
	notFoundReq.Header.Set("X-User-Id", "user-auth-2")
	res = httptest.NewRecorder()
	router.ServeHTTP(res, notFoundReq)
	if res.Code != http.StatusNotFound {
		t.Fatalf("esperado 404 para item inexistente/de outra conta, recebido %d", res.Code)
	}
}

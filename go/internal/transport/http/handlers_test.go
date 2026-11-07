package http_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Fernandes-Yuri/gateway-api/go/internal/domain"
	"github.com/Fernandes-Yuri/gateway-api/go/internal/repository"
	transporthttp "github.com/Fernandes-Yuri/gateway-api/go/internal/transport/http"
	"github.com/google/uuid"
)

// MockFamilyRepository é uma implementação em memória thread-safe de FamilyRepository
type MockFamilyRepository struct {
	mu              sync.RWMutex
	messages        map[string]domain.FamilyMessage
	bindings        map[string]domain.FamilyBinding
	accessMap       map[string]map[string]bool
	syncBindingFn   func(req *domain.SyncFamilyBindingRequest, requesterID string) (*domain.FamilyBinding, error)
	syncBindingLast *domain.SyncFamilyBindingRequest
	syncBindingUID  string
	syncBindingHits int
}

func NewMockFamilyRepository() *MockFamilyRepository {
	return &MockFamilyRepository{
		messages:  make(map[string]domain.FamilyMessage),
		bindings:  make(map[string]domain.FamilyBinding),
		accessMap: make(map[string]map[string]bool),
	}
}

func (m *MockFamilyRepository) AllowAccess(uid, patientID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.accessMap[uid]; !ok {
		m.accessMap[uid] = make(map[string]bool)
	}
	m.accessMap[uid][patientID] = true
}

func (m *MockFamilyRepository) SaveMessage(ctx context.Context, msg *domain.FamilyMessage) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if existing, ok := m.messages[msg.ID]; ok {
		if existing.SenderID == msg.SenderID && existing.PatientID == msg.PatientID {
			return nil // Idempotência
		}
		return repository.ErrConflict
	}
	m.messages[msg.ID] = *msg
	return nil
}

func (m *MockFamilyRepository) GetMessages(ctx context.Context, patientID string, limit, offset int) ([]domain.FamilyMessage, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()

	var result []domain.FamilyMessage
	for _, msg := range m.messages {
		if msg.PatientID == patientID && msg.ExpiresAt.After(time.Now().UTC()) {
			res := msg
			if res.DeletedAt != nil {
				res.MessageText = ""
				res.BodyEncrypted = nil
				res.SenderName = ""
				res.IconType = "LOVE"
			} else if res.BodyEncrypted != nil && *res.BodyEncrypted != "" {
				res.EncryptionVersion = domain.E2EECurrentVersion
				if res.MessageText == "" {
					res.MessageText = *res.BodyEncrypted
				}
			}
			result = append(result, res)
		}
	}

	if offset > len(result) {
		return []domain.FamilyMessage{}, nil
	}
	end := offset + limit
	if end > len(result) {
		end = len(result)
	}
	return result[offset:end], nil
}

func (m *MockFamilyRepository) DeleteMessage(ctx context.Context, messageID string, senderID string, patientID *string, sentAt *time.Time) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	msg, ok := m.messages[messageID]
	if !ok {
		if patientID == nil || sentAt == nil {
			return nil
		}
		now := time.Now().UTC()
		tomb := domain.FamilyMessage{
			ID:                messageID,
			PatientID:         *patientID,
			SenderID:          senderID,
			SenderName:        "",
			MessageText:       "",
			BodyEncrypted:     nil,
			EncryptionVersion: 0,
			IconType:          "LOVE",
			IsRead:            false,
			SentAt:            *sentAt,
			ExpiresAt:         sentAt.Add(24 * time.Hour),
			DeletedAt:         &now,
		}
		m.messages[messageID] = tomb
		return nil
	}

	if msg.SenderID != senderID {
		return repository.ErrForbidden
	}

	now := time.Now().UTC()
	msg.DeletedAt = &now
	msg.MessageText = ""
	msg.BodyEncrypted = nil
	msg.SenderName = ""
	msg.IconType = "LOVE"
	m.messages[messageID] = msg
	return nil
}

func (m *MockFamilyRepository) MarkMessagesAsRead(ctx context.Context, patientID string, readerID string, messageIDs ...[]string) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	for id, msg := range m.messages {
		if len(messageIDs) > 0 && messageIDs[0] != nil {
			selected := false
			for _, wanted := range messageIDs[0] {
				if wanted == id {
					selected = true
				}
			}
			if !selected {
				continue
			}
		}
		if msg.PatientID == patientID && msg.SenderID != readerID && !msg.IsRead && msg.DeletedAt == nil {
			msg.IsRead = true
			m.messages[id] = msg
		}
	}
	return nil
}

func (m *MockFamilyRepository) CheckFamilyAccess(ctx context.Context, uid string, patientID string) (bool, error) {
	if uid == patientID {
		return true, nil
	}
	m.mu.RLock()
	defer m.mu.RUnlock()
	if targets, ok := m.accessMap[uid]; ok && targets[patientID] {
		return true, nil
	}
	return false, nil
}

func (m *MockFamilyRepository) FindByConnectionCode(ctx context.Context, code string) (*domain.FamilyBinding, error) {
	clean := strings.ToUpper(strings.TrimSpace(code))
	if clean == domain.JokerConnectionCode {
		now := time.Now().UTC()
		return &domain.FamilyBinding{
			ID:                 uuid.NewSHA1(uuid.NameSpaceDNS, []byte("testfam1-joker")).String(),
			PatientID:          domain.JokerPatientUID,
			PatientName:        domain.JokerPatientName,
			ConnectionCode:     domain.JokerConnectionCode,
			Status:             "PENDING",
			CaregiverRole:      "CAREGIVER_VIEWER",
			Permissions:        []string{"ALL"},
			PatientPublicKey:   nil,
			CaregiverPublicKey: nil,
			CreatedAt:          now,
			ExpiresAt:          now.Add(3650 * 24 * time.Hour),
		}, nil
	}

	m.mu.RLock()
	defer m.mu.RUnlock()
	b, ok := m.bindings[clean]
	if !ok || b.Status != "PENDING" || b.ExpiresAt.Before(time.Now().UTC()) {
		return nil, repository.ErrNotFound
	}
	return &b, nil
}

func (m *MockFamilyRepository) AcceptInvitation(
	ctx context.Context,
	code string,
	caregiverID string,
	caregiverName string,
	caregiverRelation string,
	caregiverPublicKey *string,
) (*domain.FamilyBinding, error) {
	clean := strings.ToUpper(strings.TrimSpace(code))
	if clean == domain.JokerConnectionCode {
		if caregiverID == domain.JokerPatientUID {
			return nil, repository.ErrConflict
		}
		now := time.Now().UTC()
		return &domain.FamilyBinding{
			ID:                 uuid.New().String(),
			PatientID:          domain.JokerPatientUID,
			PatientName:        domain.JokerPatientName,
			CaregiverID:        &caregiverID,
			CaregiverName:      caregiverName,
			CaregiverRelation:  caregiverRelation,
			ConnectionCode:     domain.JokerConnectionCode,
			Status:             "ACTIVE",
			CaregiverRole:      "CAREGIVER_VIEWER",
			Permissions:        []string{"ALL"},
			CaregiverPublicKey: caregiverPublicKey,
			CreatedAt:          now,
			ExpiresAt:          now.Add(3650 * 24 * time.Hour),
		}, nil
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	b, ok := m.bindings[clean]
	if !ok {
		return nil, repository.ErrNotFound
	}
	if b.PatientID == caregiverID {
		return nil, repository.ErrConflict
	}
	b.CaregiverID = &caregiverID
	b.CaregiverName = caregiverName
	b.CaregiverRelation = caregiverRelation
	b.CaregiverPublicKey = caregiverPublicKey
	b.Status = "ACTIVE"
	m.bindings[clean] = b
	return &b, nil
}

func (m *MockFamilyRepository) PublishPublicKey(
	ctx context.Context,
	bindingID *string,
	patientID *string,
	uid string,
	publicKey string,
) (*domain.PublishFamilyPublicKeyResponse, error) {
	targetPatient := uid
	if patientID != nil && *patientID != "" {
		targetPatient = *patientID
	}
	return &domain.PublishFamilyPublicKeyResponse{
		Status:    "success",
		PatientID: targetPatient,
		PublicKey: publicKey,
	}, nil
}

func (m *MockFamilyRepository) ListBindings(ctx context.Context, userID string) ([]domain.FamilyBinding, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	var list []domain.FamilyBinding
	for _, b := range m.bindings {
		if b.PatientID == userID || (b.CaregiverID != nil && *b.CaregiverID == userID) {
			list = append(list, b)
		}
	}
	return list, nil
}

func (m *MockFamilyRepository) SyncBinding(
	ctx context.Context,
	req *domain.SyncFamilyBindingRequest,
	requesterID string,
) (*domain.FamilyBinding, error) {
	m.mu.Lock()
	m.syncBindingHits++
	m.syncBindingLast = req
	m.syncBindingUID = requesterID
	fn := m.syncBindingFn
	m.mu.Unlock()
	if fn != nil {
		return fn(req, requesterID)
	}
	return &domain.FamilyBinding{
		ID:        req.ID,
		PatientID: req.PatientID,
		Status:    req.Status,
	}, nil
}

// Struct de deserialização das fixtures canônicas
type CanonicalFixtures struct {
	Fixtures struct {
		SendE2EEMessage struct {
			Request struct {
				Method  string                 `json:"method"`
				Path    string                 `json:"path"`
				Headers map[string]string      `json:"headers"`
				Body    map[string]interface{} `json:"body"`
			} `json:"request"`
			ExpectedResponse struct {
				StatusCode int                    `json:"status_code"`
				Body       map[string]interface{} `json:"body"`
			} `json:"expected_response"`
		} `json:"send_e2ee_message"`
		GetMessagesWithTombstone struct {
			Request struct {
				Method  string            `json:"method"`
				Path    string            `json:"path"`
				Headers map[string]string `json:"headers"`
			} `json:"request"`
			ExpectedResponse struct {
				StatusCode int                    `json:"status_code"`
				SampleItem map[string]interface{} `json:"sample_item"`
			} `json:"expected_response"`
		} `json:"get_messages_with_tombstone"`
		DeleteMessageTombstone struct {
			Request struct {
				Method  string                 `json:"method"`
				Path    string                 `json:"path"`
				Headers map[string]string      `json:"headers"`
				Body    map[string]interface{} `json:"body"`
			} `json:"request"`
			ExpectedResponse struct {
				StatusCode int                    `json:"status_code"`
				Body       map[string]interface{} `json:"body"`
			} `json:"expected_response"`
		} `json:"delete_message_tombstone"`
		PublishECDHPublicKey struct {
			Request struct {
				Method  string                 `json:"method"`
				Path    string                 `json:"path"`
				Headers map[string]string      `json:"headers"`
				Body    map[string]interface{} `json:"body"`
			} `json:"request"`
			ExpectedResponse struct {
				StatusCode int                    `json:"status_code"`
				Body       map[string]interface{} `json:"body"`
			} `json:"expected_response"`
		} `json:"publish_ecdh_public_key"`
		FindByJokerCode struct {
			Request struct {
				Method  string            `json:"method"`
				Path    string            `json:"path"`
				Headers map[string]string `json:"headers"`
			} `json:"request"`
			ExpectedResponse struct {
				StatusCode int                    `json:"status_code"`
				Body       map[string]interface{} `json:"body"`
			} `json:"expected_response"`
		} `json:"find_by_joker_code"`
		AcceptInvitation struct {
			Request struct {
				Method  string                 `json:"method"`
				Path    string                 `json:"path"`
				Headers map[string]string      `json:"headers"`
				Body    map[string]interface{} `json:"body"`
			} `json:"request"`
			ExpectedResponse struct {
				StatusCode  int      `json:"status_code"`
				KeysPresent []string `json:"keys_present"`
			} `json:"expected_response"`
		} `json:"accept_invitation"`
	} `json:"fixtures"`
}

func loadFixtures(t *testing.T) CanonicalFixtures {
	t.Helper()
	candidates := []string{
		filepath.Join("..", "..", "..", "..", "tests", "contracts", "family", "fixtures_family_chat.json"),
		filepath.Join("..", "..", "..", "tests", "contracts", "family", "fixtures_family_chat.json"),
		"/home/jota1/repos/gateway-api/tests/contracts/family/fixtures_family_chat.json",
	}

	var data []byte
	var err error
	for _, path := range candidates {
		data, err = os.ReadFile(path)
		if err == nil {
			break
		}
	}
	if err != nil {
		t.Fatalf("falha ao ler arquivo de fixtures: %v", err)
	}
	var f CanonicalFixtures
	if err := json.Unmarshal(data, &f); err != nil {
		t.Fatalf("falha ao deserializar fixtures JSON: %v", err)
	}
	return f
}

func TestCanonicalFixturesParity(t *testing.T) {
	fixtures := loadFixtures(t)
	mockRepo := NewMockFamilyRepository()
	router := transporthttp.NewRouter(mockRepo)

	t.Run("Send E2EE Message Canonical Parity", func(t *testing.T) {
		fx := fixtures.Fixtures.SendE2EEMessage
		bodyMap := make(map[string]interface{})
		for k, v := range fx.Request.Body {
			bodyMap[k] = v
		}
		bodyMap["sentAt"] = time.Now().UTC().Format(time.RFC3339)
		bodyBytes, _ := json.Marshal(bodyMap)
		req := httptest.NewRequest(fx.Request.Method, fx.Request.Path, bytes.NewReader(bodyBytes))
		for k, v := range fx.Request.Headers {
			req.Header.Set(k, v)
		}

		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)

		if w.Code != fx.ExpectedResponse.StatusCode {
			t.Fatalf("Esperava status %d, obteve %d: %s", fx.ExpectedResponse.StatusCode, w.Code, w.Body.String())
		}

		var resp map[string]interface{}
		if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
			t.Fatalf("Erro ao decodificar resposta JSON: %v", err)
		}

		if resp["status"] != fx.ExpectedResponse.Body["status"] {
			t.Errorf("status divergente: esperava %v, obteve %v", fx.ExpectedResponse.Body["status"], resp["status"])
		}
		if resp["id"] != fx.ExpectedResponse.Body["id"] {
			t.Errorf("id divergente: esperava %v, obteve %v", fx.ExpectedResponse.Body["id"], resp["id"])
		}
		if int(resp["encryptionVersion"].(float64)) != int(fx.ExpectedResponse.Body["encryptionVersion"].(float64)) {
			t.Errorf("encryptionVersion divergente")
		}
	})

	t.Run("Get Messages With Tombstone Canonical Parity", func(t *testing.T) {
		fx := fixtures.Fixtures.GetMessagesWithTombstone
		req := httptest.NewRequest(fx.Request.Method, fx.Request.Path, nil)
		for k, v := range fx.Request.Headers {
			req.Header.Set(k, v)
		}

		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)

		if w.Code != fx.ExpectedResponse.StatusCode {
			t.Fatalf("Esperava status %d, obteve %d: %s", fx.ExpectedResponse.StatusCode, w.Code, w.Body.String())
		}

		var list []map[string]interface{}
		if err := json.Unmarshal(w.Body.Bytes(), &list); err != nil {
			t.Fatalf("Erro ao deserializar lista de mensagens: %v", err)
		}

		if len(list) == 0 {
			t.Fatal("Esperava pelo menos uma mensagem salva na lista")
		}

		item := list[0]
		expectedSample := fx.ExpectedResponse.SampleItem
		if item["id"] != expectedSample["id"] {
			t.Errorf("id divergente: esperava %v, obteve %v", expectedSample["id"], item["id"])
		}
		if item["patientId"] != expectedSample["patientId"] {
			t.Errorf("patientId divergente: esperava %v, obteve %v", expectedSample["patientId"], item["patientId"])
		}
		if item["bodyEncrypted"] != expectedSample["bodyEncrypted"] {
			t.Errorf("bodyEncrypted divergente: esperava %v, obteve %v", expectedSample["bodyEncrypted"], item["bodyEncrypted"])
		}
	})

	t.Run("Delete Message Tombstone Canonical Parity", func(t *testing.T) {
		fx := fixtures.Fixtures.DeleteMessageTombstone
		bodyBytes, _ := json.Marshal(fx.Request.Body)
		req := httptest.NewRequest(fx.Request.Method, fx.Request.Path, bytes.NewReader(bodyBytes))
		for k, v := range fx.Request.Headers {
			req.Header.Set(k, v)
		}

		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)

		if w.Code != fx.ExpectedResponse.StatusCode {
			t.Fatalf("Esperava status %d, obteve %d: %s", fx.ExpectedResponse.StatusCode, w.Code, w.Body.String())
		}

		var resp map[string]interface{}
		if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
			t.Fatalf("Erro ao decodificar resposta JSON: %v", err)
		}

		if resp["status"] != fx.ExpectedResponse.Body["status"] {
			t.Errorf("status divergente: esperava %v, obteve %v", fx.ExpectedResponse.Body["status"], resp["status"])
		}
		if resp["id"] != fx.ExpectedResponse.Body["id"] {
			t.Errorf("id divergente: esperava %v, obteve %v", fx.ExpectedResponse.Body["id"], resp["id"])
		}
	})

	t.Run("Publish ECDH Public Key Canonical Parity", func(t *testing.T) {
		fx := fixtures.Fixtures.PublishECDHPublicKey
		bodyBytes, _ := json.Marshal(fx.Request.Body)
		req := httptest.NewRequest(fx.Request.Method, fx.Request.Path, bytes.NewReader(bodyBytes))
		for k, v := range fx.Request.Headers {
			req.Header.Set(k, v)
		}

		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)

		if w.Code != fx.ExpectedResponse.StatusCode {
			t.Fatalf("Esperava status %d, obteve %d: %s", fx.ExpectedResponse.StatusCode, w.Code, w.Body.String())
		}

		var resp map[string]interface{}
		if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
			t.Fatalf("Erro ao decodificar resposta JSON: %v", err)
		}

		if resp["status"] != fx.ExpectedResponse.Body["status"] {
			t.Errorf("status divergente")
		}
		if resp["patientId"] != fx.ExpectedResponse.Body["patientId"] {
			t.Errorf("patientId divergente")
		}
		if resp["publicKey"] != fx.ExpectedResponse.Body["publicKey"] {
			t.Errorf("publicKey divergente")
		}
	})

	t.Run("Find By Joker Code Canonical Parity", func(t *testing.T) {
		fx := fixtures.Fixtures.FindByJokerCode
		req := httptest.NewRequest(fx.Request.Method, fx.Request.Path, nil)
		for k, v := range fx.Request.Headers {
			req.Header.Set(k, v)
		}

		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)

		if w.Code != fx.ExpectedResponse.StatusCode {
			t.Fatalf("Esperava status %d, obteve %d: %s", fx.ExpectedResponse.StatusCode, w.Code, w.Body.String())
		}

		var resp map[string]interface{}
		if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
			t.Fatalf("Erro ao decodificar resposta JSON: %v", err)
		}

		if resp["patient_id"] != fx.ExpectedResponse.Body["patient_id"] {
			t.Errorf("patient_id divergente")
		}
		if resp["patient_name"] != fx.ExpectedResponse.Body["patient_name"] {
			t.Errorf("patient_name divergente")
		}
		if resp["connection_code"] != fx.ExpectedResponse.Body["connection_code"] {
			t.Errorf("connection_code divergente")
		}
	})

	t.Run("Accept Invitation Canonical Parity", func(t *testing.T) {
		fx := fixtures.Fixtures.AcceptInvitation
		bodyBytes, _ := json.Marshal(fx.Request.Body)
		req := httptest.NewRequest(fx.Request.Method, fx.Request.Path, bytes.NewReader(bodyBytes))
		for k, v := range fx.Request.Headers {
			req.Header.Set(k, v)
		}

		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)

		if w.Code != fx.ExpectedResponse.StatusCode {
			t.Fatalf("Esperava status %d, obteve %d: %s", fx.ExpectedResponse.StatusCode, w.Code, w.Body.String())
		}

		var resp map[string]interface{}
		if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
			t.Fatalf("Erro ao decodificar resposta JSON: %v", err)
		}

		for _, key := range fx.ExpectedResponse.KeysPresent {
			if _, ok := resp[key]; !ok {
				t.Errorf("Chave esperada '%s' ausente na resposta: %v", key, resp)
			}
		}
	})
}

func TestSecurityAndEdgeCases(t *testing.T) {
	mockRepo := NewMockFamilyRepository()
	router := transporthttp.NewRouter(mockRepo)

	t.Run("Unauthenticated request fails with 401", func(t *testing.T) {
		req := httptest.NewRequest("GET", "/api/family/messages/patient_1", nil)
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		if w.Code != http.StatusUnauthorized {
			t.Errorf("Esperava 401, obteve %d", w.Code)
		}
	})

	t.Run("X-User-Id header from Rust edge proxy is respected", func(t *testing.T) {
		req := httptest.NewRequest("GET", "/api/family/messages/patient_test_uid_123", nil)
		req.Header.Set("X-User-Id", "patient_test_uid_123")
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Errorf("Esperava 200, obteve %d: %s", w.Code, w.Body.String())
		}
	})

	t.Run("Health check is public and returns 200", func(t *testing.T) {
		req := httptest.NewRequest("GET", "/health", nil)
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Errorf("Esperava 200, obteve %d", w.Code)
		}
	})

	t.Run("Invalid message UUID fails with 422", func(t *testing.T) {
		payload := map[string]interface{}{
			"id":        "not-a-valid-uuid",
			"patientId": "p1",
			"senderId":  "p1",
			"sentAt":    time.Now().UTC().Format(time.RFC3339),
		}
		b, _ := json.Marshal(payload)
		req := httptest.NewRequest("POST", "/api/family/messages/send", bytes.NewReader(b))
		req.Header.Set("X-User-Id", "p1")
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		if w.Code != http.StatusUnprocessableEntity {
			t.Errorf("Esperava 422, obteve %d", w.Code)
		}
	})

	t.Run("Wrong owner sending message fails with 403", func(t *testing.T) {
		payload := map[string]interface{}{
			"id":        uuid.New().String(),
			"patientId": "p1",
			"senderId":  "victim",
			"sentAt":    time.Now().UTC().Format(time.RFC3339),
		}
		b, _ := json.Marshal(payload)
		req := httptest.NewRequest("POST", "/api/family/messages/send", bytes.NewReader(b))
		req.Header.Set("X-User-Id", "attacker")
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		if w.Code != http.StatusForbidden {
			t.Errorf("Esperava 403, obteve %d", w.Code)
		}
	})

	t.Run("HandleMarkMessagesRead marks unread messages and broadcasts", func(t *testing.T) {
		mockRepo.AllowAccess("caregiver-reader", "patient-owner")
		msg := domain.FamilyMessage{
			ID:          "msg-read-test-1",
			PatientID:   "patient-owner",
			SenderID:    "patient-owner",
			SenderName:  "Paciente",
			MessageText: "Olá",
			IconType:    "LOVE",
			IsRead:      false,
			SentAt:      time.Now().UTC(),
			ExpiresAt:   time.Now().UTC().Add(24 * time.Hour),
		}
		_ = mockRepo.SaveMessage(context.Background(), &msg)

		payload := map[string]interface{}{
			"patientId": "patient-owner",
		}
		b, _ := json.Marshal(payload)
		req := httptest.NewRequest("POST", "/api/family/messages/read", bytes.NewReader(b))
		req.Header.Set("X-User-Id", "caregiver-reader")
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("Esperava 200, obteve %d: %s", w.Code, w.Body.String())
		}

		msgs, _ := mockRepo.GetMessages(context.Background(), "patient-owner", 10, 0)
		if len(msgs) == 0 || !msgs[0].IsRead {
			t.Errorf("Mensagem deveria estar marcada como is_read = true")
		}
	})
}

// ---------------------------------------------------------------------------
// Fase #7: POST /api/sync/family-binding (contrato do app Android no Python).
// ---------------------------------------------------------------------------
func TestSyncFamilyBindingHandler(t *testing.T) {
	mockRepo := NewMockFamilyRepository()
	router := transporthttp.NewRouter(mockRepo)

	validBody := func(mut func(map[string]interface{})) []byte {
		payload := map[string]interface{}{
			"id":                uuid.NewString(),
			"patientId":         "patient-1",
			"caregiverName":     "",
			"caregiverRelation": "",
			"connectionCode":    "abcd1234",
			"status":            "PENDING",
			"caregiverRole":     "CAREGIVER_VIEWER",
			"permissions":       []string{"care:read"},
			"createdAt":         "2026-11-06T12:00:00Z",
			"expiresAt":         "2026-11-13T12:00:00Z",
		}
		if mut != nil {
			mut(payload)
		}
		b, _ := json.Marshal(payload)
		return b
	}

	call := func(body []byte, uid string) *httptest.ResponseRecorder {
		req := httptest.NewRequest("POST", "/api/sync/family-binding", bytes.NewReader(body))
		if uid != "" {
			req.Header.Set("X-User-Id", uid)
		}
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		return w
	}

	t.Run("unauthenticated fails closed without touching repo", func(t *testing.T) {
		w := call(validBody(nil), "")
		if w.Code != http.StatusUnauthorized {
			t.Fatalf("Esperava 401, obteve %d: %s", w.Code, w.Body.String())
		}
		if mockRepo.syncBindingHits != 0 {
			t.Fatalf("Repositório não deveria ser chamado sem autenticação")
		}
	})

	t.Run("invalid JSON is 422", func(t *testing.T) {
		w := call([]byte("{invalid"), "patient-1")
		if w.Code != http.StatusUnprocessableEntity {
			t.Fatalf("Esperava 422, obteve %d", w.Code)
		}
	})

	t.Run("invalid connection code is 422", func(t *testing.T) {
		w := call(validBody(func(p map[string]interface{}) { p["connectionCode"] = "ABC" }), "patient-1")
		if w.Code != http.StatusUnprocessableEntity {
			t.Fatalf("Esperava 422, obteve %d", w.Code)
		}
	})

	t.Run("invalid role is 422", func(t *testing.T) {
		w := call(validBody(func(p map[string]interface{}) { p["caregiverRole"] = "ROOT" }), "patient-1")
		if w.Code != http.StatusUnprocessableEntity {
			t.Fatalf("Esperava 422, obteve %d", w.Code)
		}
	})

	t.Run("pending creation returns server id and normalizes payload", func(t *testing.T) {
		bindingID := uuid.NewString()
		mockRepo.mu.Lock()
		mockRepo.syncBindingFn = func(req *domain.SyncFamilyBindingRequest, requesterID string) (*domain.FamilyBinding, error) {
			return &domain.FamilyBinding{ID: bindingID, PatientID: req.PatientID, Status: req.Status}, nil
		}
		mockRepo.mu.Unlock()

		w := call(validBody(func(p map[string]interface{}) {
			p["id"] = bindingID
			p["caregiverName"] = "  <b>Ana</b>  "
			p["caregiverRelation"] = "Filha"
		}), "patient-1")
		if w.Code != http.StatusOK {
			t.Fatalf("Esperava 200, obteve %d: %s", w.Code, w.Body.String())
		}
		var resp domain.SyncFamilyBindingResponse
		if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
			t.Fatalf("Resposta inválida: %v", err)
		}
		if resp.Status != "success" || resp.ID != bindingID {
			t.Fatalf("Resposta inesperada: %+v", resp)
		}
		mockRepo.mu.RLock()
		last := mockRepo.syncBindingLast
		uid := mockRepo.syncBindingUID
		mockRepo.mu.RUnlock()
		if last == nil || uid != "patient-1" {
			t.Fatalf("Repositório recebeu requisição inesperada: %+v uid=%q", last, uid)
		}
		if last.ConnectionCode != "ABCD1234" {
			t.Fatalf("Código deveria ser normalizado, veio %q", last.ConnectionCode)
		}
		if last.CaregiverName != "Ana" {
			t.Fatalf("Nome deveria ser sanitizado/truncado, veio %q", last.CaregiverName)
		}
	})

	t.Run("revocation by stranger is 403", func(t *testing.T) {
		mockRepo.mu.Lock()
		mockRepo.syncBindingFn = func(req *domain.SyncFamilyBindingRequest, requesterID string) (*domain.FamilyBinding, error) {
			return nil, repository.ErrForbidden
		}
		mockRepo.mu.Unlock()
		w := call(validBody(func(p map[string]interface{}) { p["status"] = "REVOKED" }), "intruder")
		if w.Code != http.StatusForbidden {
			t.Fatalf("Esperava 403, obteve %d", w.Code)
		}
	})

	t.Run("revocation of unknown binding is 403 (paridade com Python)", func(t *testing.T) {
		mockRepo.mu.Lock()
		mockRepo.syncBindingFn = func(req *domain.SyncFamilyBindingRequest, requesterID string) (*domain.FamilyBinding, error) {
			return nil, repository.ErrNotFound
		}
		mockRepo.mu.Unlock()
		w := call(validBody(func(p map[string]interface{}) { p["status"] = "REVOKED" }), "patient-1")
		if w.Code != http.StatusForbidden {
			t.Fatalf("Esperava 403, obteve %d", w.Code)
		}
	})

	t.Run("conflicting invite is 409", func(t *testing.T) {
		mockRepo.mu.Lock()
		mockRepo.syncBindingFn = func(req *domain.SyncFamilyBindingRequest, requesterID string) (*domain.FamilyBinding, error) {
			return nil, repository.ErrConflict
		}
		mockRepo.mu.Unlock()
		w := call(validBody(nil), "patient-1")
		if w.Code != http.StatusConflict {
			t.Fatalf("Esperava 409, obteve %d", w.Code)
		}
	})

	t.Run("database unavailable is 503", func(t *testing.T) {
		mockRepo.mu.Lock()
		mockRepo.syncBindingFn = func(req *domain.SyncFamilyBindingRequest, requesterID string) (*domain.FamilyBinding, error) {
			return nil, repository.ErrDatabaseUnavailable
		}
		mockRepo.mu.Unlock()
		w := call(validBody(nil), "patient-1")
		if w.Code != http.StatusServiceUnavailable {
			t.Fatalf("Esperava 503, obteve %d", w.Code)
		}
	})
}

func TestSyncFamilyBindingRateLimit(t *testing.T) {
	mockRepo := NewMockFamilyRepository()
	router := transporthttp.NewRouter(mockRepo)

	body := []byte(`{"patientId":"patient-1","connectionCode":"ABCD1234","status":"PENDING","caregiverRole":"CAREGIVER_VIEWER","permissions":["care:read"]}`)
	for i := 0; i < 10; i++ {
		req := httptest.NewRequest("POST", "/api/sync/family-binding", bytes.NewReader(body))
		req.Header.Set("X-User-Id", "patient-1")
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("Requisição %d deveria ser 200, obteve %d", i+1, w.Code)
		}
	}

	req := httptest.NewRequest("POST", "/api/sync/family-binding", bytes.NewReader(body))
	req.Header.Set("X-User-Id", "patient-1")
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	if w.Code != http.StatusTooManyRequests {
		t.Fatalf("11ª requisição deveria ser 429, obteve %d", w.Code)
	}
}

func TestReadReceiptSelectsOnlyVisibleIncomingMessages(t *testing.T) {
	repo := NewMockFamilyRepository()
	repo.AllowAccess("reader", "patient")
	router := transporthttp.NewRouter(repo)
	for _, msg := range []domain.FamilyMessage{
		{ID: "visible", PatientID: "patient", SenderID: "patient"},
		{ID: "unseen", PatientID: "patient", SenderID: "patient"},
		{ID: "own", PatientID: "patient", SenderID: "reader"},
		{ID: "foreign", PatientID: "other", SenderID: "patient"},
	} {
		repo.SaveMessage(context.Background(), &msg)
	}
	for _, body := range []string{
		`{"patientId":"patient","messageIds":[]}`,
		`{"patientId":"patient","messageIds":["visible","own","foreign"]}`,
	} {
		req := httptest.NewRequest("POST", "/api/family/messages/read", strings.NewReader(body))
		req.Header.Set("X-User-Id", "reader")
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		if w.Code != 200 {
			t.Fatalf("status=%d body=%s", w.Code, w.Body.String())
		}
	}
	if !repo.messages["visible"].IsRead {
		t.Fatal("visible message must be read")
	}
	for _, id := range []string{"unseen", "own", "foreign"} {
		if repo.messages[id].IsRead {
			t.Fatalf("%s must remain unread", id)
		}
	}
}

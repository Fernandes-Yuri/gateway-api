package ws_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Fernandes-Yuri/gateway-api/go/internal/domain"
	"github.com/Fernandes-Yuri/gateway-api/go/internal/repository"
	transporthttp "github.com/Fernandes-Yuri/gateway-api/go/internal/transport/http"
	"github.com/Fernandes-Yuri/gateway-api/go/internal/transport/ws"
	"github.com/coder/websocket"
)

// mockFamilyRepo implementa repositório em memória para os testes de WebSocket
type mockFamilyRepo struct {
	mu           sync.RWMutex
	allowed      map[string]map[string]bool
	dbDown       bool
	messages     map[string]domain.FamilyMessage
	tombstonedID map[string]bool
}

func newMockFamilyRepo() *mockFamilyRepo {
	return &mockFamilyRepo{
		allowed:      make(map[string]map[string]bool),
		messages:     make(map[string]domain.FamilyMessage),
		tombstonedID: make(map[string]bool),
	}
}

func (m *mockFamilyRepo) allow(uid, patientID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.allowed[uid] == nil {
		m.allowed[uid] = make(map[string]bool)
	}
	m.allowed[uid][patientID] = true
}

func (m *mockFamilyRepo) setDBDown(down bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.dbDown = down
}

func (m *mockFamilyRepo) CheckFamilyAccess(ctx context.Context, uid string, patientID string) (bool, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	if m.dbDown {
		return false, repository.ErrDatabaseUnavailable
	}
	if uid == patientID {
		return true, nil
	}
	if m.allowed[uid] != nil && m.allowed[uid][patientID] {
		return true, nil
	}
	return false, nil
}

func (m *mockFamilyRepo) SaveMessage(ctx context.Context, msg *domain.FamilyMessage) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.dbDown {
		return repository.ErrDatabaseUnavailable
	}
	m.messages[msg.ID] = *msg
	return nil
}

func (m *mockFamilyRepo) GetMessages(ctx context.Context, patientID string, limit, offset int) ([]domain.FamilyMessage, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	var res []domain.FamilyMessage
	for _, msg := range m.messages {
		if msg.PatientID == patientID {
			res = append(res, msg)
		}
	}
	return res, nil
}

func (m *mockFamilyRepo) DeleteMessage(ctx context.Context, messageID string, senderID string, patientID *string, sentAt *time.Time) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.dbDown {
		return repository.ErrDatabaseUnavailable
	}
	m.tombstonedID[messageID] = true
	return nil
}

func (m *mockFamilyRepo) MarkMessagesAsRead(ctx context.Context, patientID string, readerID string, messageIDs ...[]string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.dbDown {
		return repository.ErrDatabaseUnavailable
	}
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
		if msg.PatientID == patientID && msg.SenderID != readerID {
			msg.IsRead = true
			m.messages[id] = msg
		}
	}
	return nil
}

func (m *mockFamilyRepo) FindByConnectionCode(ctx context.Context, code string) (*domain.FamilyBinding, error) {
	return nil, repository.ErrNotFound
}

func (m *mockFamilyRepo) SyncBinding(ctx context.Context, req *domain.SyncFamilyBindingRequest, requesterID string) (*domain.FamilyBinding, error) {
	return nil, repository.ErrNotFound
}

func (m *mockFamilyRepo) AcceptInvitation(ctx context.Context, code string, caregiverID string, caregiverName string, caregiverRelation string, caregiverPublicKey *string) (*domain.FamilyBinding, error) {
	return nil, repository.ErrNotFound
}

func (m *mockFamilyRepo) PublishPublicKey(ctx context.Context, bindingID *string, patientID *string, uid string, publicKey string) (*domain.PublishFamilyPublicKeyResponse, error) {
	return nil, repository.ErrNotFound
}

func (m *mockFamilyRepo) ListBindings(ctx context.Context, userID string) ([]domain.FamilyBinding, error) {
	return nil, nil
}

// dialWS auxilia a conexão WebSocket com um servidor de teste
func dialWS(ctx context.Context, t *testing.T, serverURL string, pathWithQuery string, headers http.Header) *websocket.Conn {
	t.Helper()
	wsURL := "ws" + strings.TrimPrefix(serverURL, "http") + pathWithQuery
	opts := &websocket.DialOptions{
		HTTPHeader: headers,
	}
	conn, resp, err := websocket.Dial(ctx, wsURL, opts)
	if err != nil {
		if resp != nil {
			t.Fatalf("Dial falhou com status %d: %v", resp.StatusCode, err)
		}
		t.Fatalf("Dial falhou: %v", err)
	}
	return conn
}

func waitForClientCount(t *testing.T, hub *ws.Hub, patientID string, expected int, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if hub.ClientCount(patientID) == expected {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("Timeout aguardando ClientCount(%s) == %d (atual: %d)", patientID, expected, hub.ClientCount(patientID))
}

func waitForTotalClients(t *testing.T, hub *ws.Hub, expected int, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if hub.TotalClients() == expected {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("Timeout aguardando TotalClients() == %d (atual: %d)", expected, hub.TotalClients())
}

// TestWebSocketHandshakeAndAuth valida handshake e extração de identidade
func TestWebSocketHandshakeAndAuth(t *testing.T) {
	repo := newMockFamilyRepo()
	repo.allow("caregiver_456", "patient_123")
	hub := ws.NewHub()
	handler := ws.NewHandler(hub, repo)

	server := httptest.NewServer(http.HandlerFunc(handler.ServeWS))
	defer server.Close()

	t.Run("Sem autenticacao falha com 401 Unauthorized", func(t *testing.T) {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()

		wsURL := "ws" + strings.TrimPrefix(server.URL, "http") + "/ws/family?patientId=patient_123"
		_, resp, err := websocket.Dial(ctx, wsURL, nil)
		if err == nil {
			t.Fatal("Esperava falha de autenticação, mas dial teve sucesso")
		}
		if resp == nil || resp.StatusCode != http.StatusUnauthorized {
			t.Fatalf("Esperava status 401, obteve: %v", resp)
		}
	})

	t.Run("Sem patientId falha com 422 Unprocessable Entity", func(t *testing.T) {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()

		wsURL := "ws" + strings.TrimPrefix(server.URL, "http") + "/ws/family?token=patient_123"
		_, resp, err := websocket.Dial(ctx, wsURL, nil)
		if err == nil {
			t.Fatal("Esperava erro de patientId ausente, mas dial teve sucesso")
		}
		if resp == nil || resp.StatusCode != http.StatusUnprocessableEntity {
			t.Fatalf("Esperava status 422, obteve: %v", resp)
		}
	})

	t.Run("Usuario sem vinculo familiar falha com 403 Forbidden", func(t *testing.T) {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()

		wsURL := "ws" + strings.TrimPrefix(server.URL, "http") + "/ws/family?token=unauthorized_user&patientId=patient_123"
		_, resp, err := websocket.Dial(ctx, wsURL, nil)
		if err == nil {
			t.Fatal("Esperava erro 403, mas dial teve sucesso")
		}
		if resp == nil || resp.StatusCode != http.StatusForbidden {
			t.Fatalf("Esperava status 403, obteve: %v", resp)
		}
	})

	t.Run("Banco de dados indisponivel falha com 503", func(t *testing.T) {
		repo.setDBDown(true)
		defer repo.setDBDown(false)

		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()

		wsURL := "ws" + strings.TrimPrefix(server.URL, "http") + "/ws/family?token=patient_123&patientId=patient_123"
		_, resp, err := websocket.Dial(ctx, wsURL, nil)
		if err == nil {
			t.Fatal("Esperava 503, mas dial teve sucesso")
		}
		if resp == nil || resp.StatusCode != http.StatusServiceUnavailable {
			t.Fatalf("Esperava status 503, obteve: %v", resp)
		}
	})

	t.Run("Paciente autenticado por token na query estabelece conexao 101", func(t *testing.T) {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()

		conn := dialWS(ctx, t, server.URL, "/ws/family?token=<FIREBASE_JWT_PATIENT>&patientId=patient_test_uid_123", nil)
		defer conn.Close(websocket.StatusNormalClosure, "test finished")

		waitForClientCount(t, hub, "patient_test_uid_123", 1, 1*time.Second)
	})

	t.Run("Cuidador autenticado por header X-User-Id estabelece conexao 101", func(t *testing.T) {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()

		header := http.Header{}
		header.Set("X-User-Id", "caregiver_456")

		conn := dialWS(ctx, t, server.URL, "/ws/family?patientId=patient_123", header)
		defer conn.Close(websocket.StatusNormalClosure, "test finished")

		waitForClientCount(t, hub, "patient_123", 1, 1*time.Second)
		if !hub.IsUserConnected("patient_123", "caregiver_456") {
			t.Fatal("Esperava caregiver_456 registrado na sala patient_123")
		}
	})
}

// TestBroadcastFamilyMessage valida o envio de mensagens para membros da família
func TestBroadcastFamilyMessage(t *testing.T) {
	repo := newMockFamilyRepo()
	repo.allow("caregiver_456", "patient_123")
	hub := ws.NewHub()
	handler := ws.NewHandler(hub, repo)

	server := httptest.NewServer(http.HandlerFunc(handler.ServeWS))
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// Conecta o Paciente
	connPatient := dialWS(ctx, t, server.URL, "/ws/family?token=patient_123&patientId=patient_123", nil)
	defer connPatient.Close(websocket.StatusNormalClosure, "")

	// Conecta o Cuidador
	connCaregiver := dialWS(ctx, t, server.URL, "/ws/family?token=caregiver_456&patientId=patient_123", nil)
	defer connCaregiver.Close(websocket.StatusNormalClosure, "")

	waitForClientCount(t, hub, "patient_123", 2, 1*time.Second)

	// Dispara o broadcast
	encryptedBlob := `{"v":1,"iv":"dGVzdGVpdmJhc2U2NA==","ciphertext":"Y2lwaGVydGV4dGRhdGE="}`
	msg := &domain.FamilyMessage{
		ID:                "msg-uuid-broadcast-001",
		PatientID:         "patient_123",
		SenderID:          "caregiver_456",
		SenderName:        "Maria Cuidadora",
		BodyEncrypted:     &encryptedBlob,
		EncryptionVersion: 1,
		IconType:          "HEART",
		SentAt:            time.Now().UTC(),
		ExpiresAt:         time.Now().UTC().Add(24 * time.Hour),
	}

	err := hub.BroadcastFamilyMessage("patient_123", msg)
	if err != nil {
		t.Fatalf("BroadcastFamilyMessage falhou: %v", err)
	}

	// Ambos os clientes devem receber a mensagem
	for idx, conn := range []*websocket.Conn{connPatient, connCaregiver} {
		readCtx, readCancel := context.WithTimeout(ctx, 2*time.Second)
		_, payload, err := conn.Read(readCtx)
		readCancel()
		if err != nil {
			t.Fatalf("Cliente [%d] falhou ao ler mensagem: %v", idx, err)
		}

		var ev ws.Event
		if err := json.Unmarshal(payload, &ev); err != nil {
			t.Fatalf("Cliente [%d] erro ao desserializar JSON: %v", idx, err)
		}

		if ev.Type != "FAMILY_MESSAGE" {
			t.Fatalf("Cliente [%d] tipo inesperado: %s", idx, ev.Type)
		}

		dataBytes, _ := json.Marshal(ev.Data)
		var received domain.FamilyMessage
		_ = json.Unmarshal(dataBytes, &received)

		if received.ID != msg.ID {
			t.Errorf("Cliente [%d] id incorreto: obteve %s, esperava %s", idx, received.ID, msg.ID)
		}
		if received.PatientID != "patient_123" {
			t.Errorf("Cliente [%d] patientId incorreto: %s", idx, received.PatientID)
		}
		if received.SenderID != "caregiver_456" {
			t.Errorf("Cliente [%d] senderId incorreto: %s", idx, received.SenderID)
		}
	}
}

// TestBroadcastMessageDeleted valida o envio do evento de tombstone
func TestBroadcastMessageDeleted(t *testing.T) {
	repo := newMockFamilyRepo()
	hub := ws.NewHub()
	handler := ws.NewHandler(hub, repo)

	server := httptest.NewServer(http.HandlerFunc(handler.ServeWS))
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	conn := dialWS(ctx, t, server.URL, "/ws/family?token=patient_del&patientId=patient_del", nil)
	defer conn.Close(websocket.StatusNormalClosure, "")

	waitForClientCount(t, hub, "patient_del", 1, 1*time.Second)

	err := hub.BroadcastMessageDeleted("patient_del", "msg-deleted-123")
	if err != nil {
		t.Fatalf("BroadcastMessageDeleted falhou: %v", err)
	}

	readCtx, readCancel := context.WithTimeout(ctx, 2*time.Second)
	_, payload, err := conn.Read(readCtx)
	readCancel()
	if err != nil {
		t.Fatalf("Falha ao ler frame de deleção: %v", err)
	}

	var ev struct {
		Type string                `json:"type"`
		Data ws.MessageDeletedData `json:"data"`
	}
	if err := json.Unmarshal(payload, &ev); err != nil {
		t.Fatalf("Erro ao decodificar JSON: %v", err)
	}

	if ev.Type != "MESSAGE_DELETED" {
		t.Fatalf("Tipo inesperado: esperava MESSAGE_DELETED, obteve %s", ev.Type)
	}
	if ev.Data.ID != "msg-deleted-123" {
		t.Fatalf("ID deletado incorreto: %s", ev.Data.ID)
	}
	if ev.Data.PatientID != "patient_del" {
		t.Fatalf("PatientID incorreto: %s", ev.Data.PatientID)
	}
}

// TestFamilyIsolation valida isolamento rígido entre famílias diferentes
func TestFamilyIsolation(t *testing.T) {
	repo := newMockFamilyRepo()
	hub := ws.NewHub()
	handler := ws.NewHandler(hub, repo)

	server := httptest.NewServer(http.HandlerFunc(handler.ServeWS))
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// 1. Mensagem enviada para Família Alpha não pode vazar para Família Beta
	connAlpha := dialWS(ctx, t, server.URL, "/ws/family?token=patient_alpha&patientId=patient_alpha", nil)
	defer connAlpha.Close(websocket.StatusNormalClosure, "")

	connBeta := dialWS(ctx, t, server.URL, "/ws/family?token=patient_beta&patientId=patient_beta", nil)
	defer connBeta.Close(websocket.StatusNormalClosure, "")

	waitForClientCount(t, hub, "patient_alpha", 1, 1*time.Second)
	waitForClientCount(t, hub, "patient_beta", 1, 1*time.Second)

	msgAlpha := &domain.FamilyMessage{
		ID:        "msg-alpha-only",
		PatientID: "patient_alpha",
		SenderID:  "patient_alpha",
		IconType:  "SUN",
		SentAt:    time.Now().UTC(),
		ExpiresAt: time.Now().UTC().Add(24 * time.Hour),
	}
	_ = hub.BroadcastFamilyMessage("patient_alpha", msgAlpha)

	// Alpha deve receber
	readCtxA, cancelA := context.WithTimeout(ctx, 1*time.Second)
	_, payloadA, errA := connAlpha.Read(readCtxA)
	cancelA()
	if errA != nil {
		t.Fatalf("Alpha deveria ter recebido a mensagem: %v", errA)
	}
	if !strings.Contains(string(payloadA), "msg-alpha-only") {
		t.Fatalf("Mensagem inesperada para Alpha: %s", string(payloadA))
	}

	// Beta NÃO pode receber a mensagem de Alpha (timeout no read comprova ausência)
	readCtxB, cancelB := context.WithTimeout(ctx, 100*time.Millisecond)
	_, _, errB := connBeta.Read(readCtxB)
	cancelB()
	if errB == nil {
		t.Fatal("Vazamento de privacidade: Beta recebeu mensagem da Família Alpha!")
	}

	// 2. Tombstone enviado para Família Delta não pode vazar para Família Gamma
	connGamma := dialWS(ctx, t, server.URL, "/ws/family?token=patient_gamma&patientId=patient_gamma", nil)
	defer connGamma.Close(websocket.StatusNormalClosure, "")

	connDelta := dialWS(ctx, t, server.URL, "/ws/family?token=patient_delta&patientId=patient_delta", nil)
	defer connDelta.Close(websocket.StatusNormalClosure, "")

	waitForClientCount(t, hub, "patient_gamma", 1, 1*time.Second)
	waitForClientCount(t, hub, "patient_delta", 1, 1*time.Second)

	_ = hub.BroadcastMessageDeleted("patient_delta", "del-delta-only")

	// Delta deve receber
	readCtxD, cancelD := context.WithTimeout(ctx, 1*time.Second)
	_, payloadD, errD := connDelta.Read(readCtxD)
	cancelD()
	if errD != nil {
		t.Fatalf("Delta deveria ter recebido o tombstone: %v", errD)
	}
	if !strings.Contains(string(payloadD), "del-delta-only") {
		t.Fatalf("Tombstone inesperado para Delta: %s", string(payloadD))
	}

	// Gamma NÃO pode receber o tombstone de Delta
	readCtxG, cancelG := context.WithTimeout(ctx, 100*time.Millisecond)
	_, _, errG := connGamma.Read(readCtxG)
	cancelG()
	if errG == nil {
		t.Fatal("Vazamento de privacidade: Gamma recebeu tombstone da Família Delta!")
	}
}

// TestClientDisconnectionCleanUnregister valida que desconexão remove cliente do Hub
func TestClientDisconnectionCleanUnregister(t *testing.T) {
	repo := newMockFamilyRepo()
	hub := ws.NewHub()
	handler := ws.NewHandler(hub, repo)

	server := httptest.NewServer(http.HandlerFunc(handler.ServeWS))
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	conn := dialWS(ctx, t, server.URL, "/ws/family?token=patient_disc&patientId=patient_disc", nil)

	waitForClientCount(t, hub, "patient_disc", 1, 1*time.Second)

	// Encerra a conexão voluntariamente pelo cliente
	_ = conn.Close(websocket.StatusNormalClosure, "client closing")

	// Aguarda o readPump detectar o encerramento e desregistrar
	waitForClientCount(t, hub, "patient_disc", 0, 1*time.Second)
	waitForTotalClients(t, hub, 0, 1*time.Second)
}

// TestHubShutdown valida o fechamento gracioso de todas as conexões
func TestHubShutdown(t *testing.T) {
	repo := newMockFamilyRepo()
	hub := ws.NewHub()
	handler := ws.NewHandler(hub, repo)

	server := httptest.NewServer(http.HandlerFunc(handler.ServeWS))
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	conn1 := dialWS(ctx, t, server.URL, "/ws/family?token=p1&patientId=p1", nil)
	conn2 := dialWS(ctx, t, server.URL, "/ws/family?token=p2&patientId=p2", nil)

	waitForTotalClients(t, hub, 2, 1*time.Second)

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer shutdownCancel()

	if err := hub.Shutdown(shutdownCtx); err != nil {
		t.Fatalf("Shutdown do Hub falhou: %v", err)
	}

	if hub.TotalClients() != 0 {
		t.Fatalf("Após Shutdown, TotalClients deve ser 0, obteve %d", hub.TotalClients())
	}

	// Verifica se os sockets foram fechados pelo servidor
	readCtx, readCancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer readCancel()
	_, _, err1 := conn1.Read(readCtx)
	_, _, err2 := conn2.Read(readCtx)

	if err1 == nil || err2 == nil {
		t.Fatalf("Esperava erro de conexão encerrada nos clientes após Shutdown")
	}
}

// TestHTTPIntegration_SendAndTombstoneBroadcast valida a integração fim-a-fim entre HTTP e WebSocket
func TestHTTPIntegration_SendAndTombstoneBroadcast(t *testing.T) {
	repo := newMockFamilyRepo()
	repo.allow("caregiver_test_uid_456", "patient_test_uid_123")

	hub := ws.NewHub()
	go hub.Run()
	defer hub.Shutdown(context.Background())

	router := transporthttp.NewRouter(repo, hub)
	server := httptest.NewServer(router)
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// 1. Paciente conecta no WebSocket /ws/family
	wsConn := dialWS(ctx, t, server.URL, "/ws/family?token=<FIREBASE_JWT_PATIENT>&patientId=patient_test_uid_123", nil)
	defer wsConn.Close(websocket.StatusNormalClosure, "")

	waitForClientCount(t, hub, "patient_test_uid_123", 1, 1*time.Second)

	// 2. Cuidador envia mensagem via HTTP POST /api/family/messages/send
	sendURL := server.URL + "/api/family/messages/send"
	sendPayload := map[string]interface{}{
		"id":          "11111111-2222-3333-4444-555555555555",
		"patientId":   "patient_test_uid_123",
		"senderId":    "caregiver_test_uid_456",
		"senderName":  "Dra. Julia",
		"messageText": "Olá! Seu remédio foi tomado hoje?",
		"iconType":    "HEART",
		"sentAt":      time.Now().UTC().Format(time.RFC3339Nano),
	}
	sendBytes, _ := json.Marshal(sendPayload)

	httpReq, _ := http.NewRequest("POST", sendURL, bytes.NewReader(sendBytes))
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", "Bearer <FIREBASE_JWT_CAREGIVER>")

	httpResp, err := http.DefaultClient.Do(httpReq)
	if err != nil {
		t.Fatalf("Erro na requisição HTTP send: %v", err)
	}
	if httpResp.StatusCode != http.StatusOK {
		t.Fatalf("HTTP send retornou status inesperado: %d", httpResp.StatusCode)
	}
	httpResp.Body.Close()

	// 3. Paciente deve receber o evento FAMILY_MESSAGE no WebSocket em < 2ms
	readCtx, readCancel := context.WithTimeout(ctx, 2*time.Second)
	_, wsPayload, err := wsConn.Read(readCtx)
	readCancel()
	if err != nil {
		t.Fatalf("Falha ao receber FAMILY_MESSAGE via WebSocket: %v", err)
	}

	var msgEv ws.Event
	if err := json.Unmarshal(wsPayload, &msgEv); err != nil {
		t.Fatalf("Erro ao decodificar payload WebSocket: %v", err)
	}

	if msgEv.Type != "FAMILY_MESSAGE" {
		t.Fatalf("Esperava type=FAMILY_MESSAGE, obteve %s", msgEv.Type)
	}

	dataMap, ok := msgEv.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("Data do evento inválido: %T", msgEv.Data)
	}
	if dataMap["id"] != "11111111-2222-3333-4444-555555555555" {
		t.Fatalf("ID da mensagem divergente no WebSocket: %v", dataMap["id"])
	}

	// 4. Cuidador apaga mensagem via HTTP POST /api/family/message/delete
	delURL := server.URL + "/api/family/message/delete"
	delPayload := map[string]interface{}{
		"id":        "11111111-2222-3333-4444-555555555555",
		"patientId": "patient_test_uid_123",
	}
	delBytes, _ := json.Marshal(delPayload)

	delReq, _ := http.NewRequest("POST", delURL, bytes.NewReader(delBytes))
	delReq.Header.Set("Content-Type", "application/json")
	delReq.Header.Set("Authorization", "Bearer <FIREBASE_JWT_CAREGIVER>")

	delResp, err := http.DefaultClient.Do(delReq)
	if err != nil {
		t.Fatalf("Erro na requisição HTTP delete: %v", err)
	}
	if delResp.StatusCode != http.StatusOK {
		t.Fatalf("HTTP delete retornou status inesperado: %d", delResp.StatusCode)
	}
	delResp.Body.Close()

	// 5. Paciente deve receber o evento MESSAGE_DELETED no WebSocket imediatamente
	readCtxDel, readCancelDel := context.WithTimeout(ctx, 2*time.Second)
	_, wsDelPayload, err := wsConn.Read(readCtxDel)
	readCancelDel()
	if err != nil {
		t.Fatalf("Falha ao receber MESSAGE_DELETED via WebSocket: %v", err)
	}

	var delEv struct {
		Type string                `json:"type"`
		Data ws.MessageDeletedData `json:"data"`
	}
	if err := json.Unmarshal(wsDelPayload, &delEv); err != nil {
		t.Fatalf("Erro ao decodificar JSON de tombstone: %v", err)
	}

	if delEv.Type != "MESSAGE_DELETED" {
		t.Fatalf("Esperava type=MESSAGE_DELETED, obteve %s", delEv.Type)
	}
	if delEv.Data.ID != "11111111-2222-3333-4444-555555555555" {
		t.Fatalf("ID do tombstone divergente: %s", delEv.Data.ID)
	}
	if delEv.Data.PatientID != "patient_test_uid_123" {
		t.Fatalf("PatientID do tombstone divergente: %s", delEv.Data.PatientID)
	}
}

// TestPingPongHeartbeat valida que pings enviados periodicamente mantêm a conexão íntegra
func TestPingPongHeartbeat(t *testing.T) {
	repo := newMockFamilyRepo()
	hub := ws.NewHub()
	fastCfg := ws.ClientConfig{
		PingInterval: 30 * time.Millisecond,
		WriteTimeout: 1 * time.Second,
		ReadLimit:    65536,
	}
	handler := ws.NewHandler(hub, repo, fastCfg)

	server := httptest.NewServer(http.HandlerFunc(handler.ServeWS))
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	conn := dialWS(ctx, t, server.URL, "/ws/family?token=patient_ping&patientId=patient_ping", nil)
	defer conn.Close(websocket.StatusNormalClosure, "")

	waitForClientCount(t, hub, "patient_ping", 1, 1*time.Second)

	// Inicia leitura no cliente para processamento automático de frames de controle pong
	doneChan := make(chan struct{})
	go func() {
		defer close(doneChan)
		for {
			_, _, err := conn.Read(ctx)
			if err != nil {
				return
			}
		}
	}()

	// Aguarda múltiplos ciclos de ping (pelo menos 3 a 4 pings em 150ms)
	time.Sleep(150 * time.Millisecond)

	// Conexão continua 100% ativa e saudável
	if count := hub.ClientCount("patient_ping"); count != 1 {
		t.Fatalf("Esperava conexão ativa após ciclos de ping/pong, obteve %d", count)
	}
}

// TestConcurrentRegisterUnregisterBroadcast estressa o Hub sob concorrência intensa
func TestConcurrentRegisterUnregisterBroadcast(t *testing.T) {
	hub := ws.NewHub()
	var wg sync.WaitGroup

	numGoroutines := 20
	iterations := 50

	for g := 0; g < numGoroutines; g++ {
		wg.Add(1)
		go func(gid int) {
			defer wg.Done()
			patientID := fmt.Sprintf("pat_%d", gid%5)
			userID := fmt.Sprintf("usr_%d", gid)

			for i := 0; i < iterations; i++ {
				client := ws.NewClient(hub, nil, userID, patientID)
				hub.Register(client)

				_ = hub.BroadcastFamilyMessage(patientID, &domain.FamilyMessage{
					ID:        "msg-concurrent",
					PatientID: patientID,
				})
				_ = hub.BroadcastMessageDeleted(patientID, "msg-concurrent")

				_ = hub.ClientCount(patientID)
				_ = hub.TotalClients()
				_ = hub.IsUserConnected(patientID, userID)

				hub.Unregister(client)
			}
		}(g)
	}

	wg.Wait()

	if total := hub.TotalClients(); total != 0 {
		t.Fatalf("Esperava 0 conexões ao final do teste de concorrência, obteve %d", total)
	}
}

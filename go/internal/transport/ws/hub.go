package ws

import (
	"context"
	"encoding/json"
	"sync"

	"github.com/Fernandes-Yuri/gateway-api/go/internal/domain"
	"github.com/coder/websocket"
)

// Event envelope padronizado para mensagens trafegadas no WebSocket
type Event struct {
	Type string `json:"type"`
	Data any    `json:"data"`
}

// MessageDeletedData payload do evento MESSAGE_DELETED
type MessageDeletedData struct {
	ID             string `json:"id"`
	PatientID      string `json:"patientId"`
	PatientIDSnake string `json:"patient_id,omitempty"`
}

// Hub gerencia as conexões WebSocket ativas, indexadas por patientId e userId de forma thread-safe
type Hub struct {
	mu       sync.RWMutex
	// patientID -> userID -> set de *Client
	clients  map[string]map[string]map[*Client]bool
	done     chan struct{}
	stopOnce sync.Once
}

// NewHub instancia um novo Hub para controle de tempo real
func NewHub() *Hub {
	return &Hub{
		clients: make(map[string]map[string]map[*Client]bool),
		done:    make(chan struct{}),
	}
}

// Register adiciona um cliente ao Hub sob o respectivo patientID e userID
func (h *Hub) Register(c *Client) {
	if c == nil {
		return
	}
	h.mu.Lock()
	defer h.mu.Unlock()

	patientClients, ok := h.clients[c.patientID]
	if !ok {
		patientClients = make(map[string]map[*Client]bool)
		h.clients[c.patientID] = patientClients
	}

	userClients, ok := patientClients[c.userID]
	if !ok {
		userClients = make(map[*Client]bool)
		patientClients[c.userID] = userClients
	}

	userClients[c] = true
}

// Unregister remove um cliente do Hub e limpa entradas vazias do mapa
func (h *Hub) Unregister(c *Client) {
	if c == nil {
		return
	}
	h.mu.Lock()
	defer h.mu.Unlock()

	patientClients, ok := h.clients[c.patientID]
	if !ok {
		return
	}

	userClients, ok := patientClients[c.userID]
	if !ok {
		return
	}

	delete(userClients, c)
	if len(userClients) == 0 {
		delete(patientClients, c.userID)
	}
	if len(patientClients) == 0 {
		delete(h.clients, c.patientID)
	}
}

// BroadcastFamilyMessage envia o evento FAMILY_MESSAGE a todos os membros conectados da família
func (h *Hub) BroadcastFamilyMessage(patientID string, msg *domain.FamilyMessage) error {
	event := Event{
		Type: "FAMILY_MESSAGE",
		Data: msg,
	}
	return h.BroadcastToFamily(patientID, event)
}

// BroadcastMessageDeleted envia o evento MESSAGE_DELETED informando remoção em tempo real
func (h *Hub) BroadcastMessageDeleted(patientID string, messageID string) error {
	event := Event{
		Type: "MESSAGE_DELETED",
		Data: MessageDeletedData{
			ID:             messageID,
			PatientID:      patientID,
			PatientIDSnake: patientID,
		},
	}
	return h.BroadcastToFamily(patientID, event)
}

// BroadcastToFamily envia qualquer evento estruturado aos membros de uma família específica
func (h *Hub) BroadcastToFamily(patientID string, event any) error {
	data, err := json.Marshal(event)
	if err != nil {
		return err
	}

	h.mu.RLock()
	defer h.mu.RUnlock()

	patientClients, ok := h.clients[patientID]
	if !ok {
		return nil
	}

	for _, userClients := range patientClients {
		for c := range userClients {
			select {
			case c.send <- data:
			default:
				// Buffer cheio: descarta quadro para não atrasar os demais membros
			}
		}
	}

	return nil
}

// ClientCount retorna a quantidade de clientes conectados para uma família
func (h *Hub) ClientCount(patientID string) int {
	h.mu.RLock()
	defer h.mu.RUnlock()

	patientClients, ok := h.clients[patientID]
	if !ok {
		return 0
	}

	count := 0
	for _, userClients := range patientClients {
		count += len(userClients)
	}
	return count
}

// TotalClients retorna o número total de conexões WebSocket ativas no servidor
func (h *Hub) TotalClients() int {
	h.mu.RLock()
	defer h.mu.RUnlock()

	count := 0
	for _, patientClients := range h.clients {
		for _, userClients := range patientClients {
			count += len(userClients)
		}
	}
	return count
}

// IsUserConnected verifica se determinado usuário possui conexões ativas na sala do paciente
func (h *Hub) IsUserConnected(patientID, userID string) bool {
	h.mu.RLock()
	defer h.mu.RUnlock()

	patientClients, ok := h.clients[patientID]
	if !ok {
		return false
	}

	userClients, ok := patientClients[userID]
	if !ok {
		return false
	}

	return len(userClients) > 0
}

// Run mantém o Hub operacional até o recebimento de sinal de encerramento
func (h *Hub) Run() {
	<-h.done
}

// Shutdown fecha todas as conexões ativas com fechamento normal e encerra o Hub
func (h *Hub) Shutdown(ctx context.Context) error {
	h.mu.Lock()
	defer h.mu.Unlock()

	h.stopOnce.Do(func() {
		close(h.done)
	})

	for _, patientClients := range h.clients {
		for _, userClients := range patientClients {
			for c := range userClients {
				c.Close(websocket.StatusNormalClosure, "server shutting down")
			}
		}
	}
	clear(h.clients)
	return nil
}

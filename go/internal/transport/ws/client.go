package ws

import (
	"context"
	"sync"
	"time"

	"github.com/coder/websocket"
)

// ClientConfig define os parâmetros operacionais da conexão WebSocket
type ClientConfig struct {
	PingInterval time.Duration
	WriteTimeout time.Duration
	ReadLimit    int64
}

// DefaultClientConfig configuração padrão de produção (30s ping, 10s write timeout, 64KB frame limit)
var DefaultClientConfig = ClientConfig{
	PingInterval: 30 * time.Second,
	WriteTimeout: 10 * time.Second,
	ReadLimit:    65536,
}

// Client encapsula a conexão WebSocket de um usuário familiar conectado
type Client struct {
	hub       *Hub
	conn      *websocket.Conn
	userID    string
	patientID string
	send      chan []byte
	ctx       context.Context
	cancel    context.CancelFunc
	closeOnce sync.Once
	cfg       ClientConfig
}

// NewClient cria uma nova instância de Client
func NewClient(hub *Hub, conn *websocket.Conn, userID, patientID string, cfgs ...ClientConfig) *Client {
	cfg := DefaultClientConfig
	if len(cfgs) > 0 {
		cfg = cfgs[0]
	}

	ctx, cancel := context.WithCancel(context.Background())
	return &Client{
		hub:       hub,
		conn:      conn,
		userID:    userID,
		patientID: patientID,
		send:      make(chan []byte, 128),
		ctx:       ctx,
		cancel:    cancel,
		cfg:       cfg,
	}
}

// UserID retorna o identificador do usuário conectado
func (c *Client) UserID() string {
	return c.userID
}

// PatientID retorna o identificador do paciente associado à conexão
func (c *Client) PatientID() string {
	return c.patientID
}

// Close encerra a conexão WebSocket e cancela o contexto de trabalho de forma atômica
func (c *Client) Close(code websocket.StatusCode, reason string) {
	c.closeOnce.Do(func() {
		c.cancel()
		if c.conn != nil {
			_ = c.conn.Close(code, reason)
		}
	})
}

// WritePump bombeia dados do canal send e emite pings periódicos mantendo a sessão viva
func (c *Client) WritePump() {
	c.writePump()
}

func (c *Client) writePump() {
	ticker := time.NewTicker(c.cfg.PingInterval)
	defer func() {
		ticker.Stop()
		c.Close(websocket.StatusNormalClosure, "write pump closed")
	}()

	for {
		select {
		case <-c.ctx.Done():
			return
		case msg, ok := <-c.send:
			if !ok {
				return
			}
			writeCtx, writeCancel := context.WithTimeout(c.ctx, c.cfg.WriteTimeout)
			err := c.conn.Write(writeCtx, websocket.MessageText, msg)
			writeCancel()
			if err != nil {
				return
			}
		case <-ticker.C:
			pingCtx, pingCancel := context.WithTimeout(c.ctx, c.cfg.WriteTimeout)
			err := c.conn.Ping(pingCtx)
			pingCancel()
			if err != nil {
				return
			}
		}
	}
}

// ReadPump lê dados da conexão e gerencia cancelamento limpo e desregistro ao desconectar
func (c *Client) ReadPump() {
	c.readPump()
}

func (c *Client) readPump() {
	defer func() {
		c.hub.Unregister(c)
		c.Close(websocket.StatusNormalClosure, "read pump closed")
	}()

	c.conn.SetReadLimit(c.cfg.ReadLimit)

	for {
		_, _, err := c.conn.Read(c.ctx)
		if err != nil {
			return
		}
	}
}

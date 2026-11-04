package main

import (
	"context"
	"errors"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"github.com/Fernandes-Yuri/gateway-api/go/internal/repository"
	transporthttp "github.com/Fernandes-Yuri/gateway-api/go/internal/transport/http"
	transportws "github.com/Fernandes-Yuri/gateway-api/go/internal/transport/ws"
	"github.com/jackc/pgx/v5/pgxpool"
)

func main() {
	log.Println("[braga-chat] Inicializando serviço Go (SPEC-13 & D47)...")

	// 1. Configurações via variáveis de ambiente
	dbURL := os.Getenv("DATABASE_URL")
	socketPath := os.Getenv("SOCKET_PATH")
	listenAddr := os.Getenv("LISTEN_ADDR")
	if listenAddr == "" {
		listenAddr = ":8082"
	}

	maxConns := 5
	if mc := os.Getenv("POOL_MAX_CONNS"); mc != "" {
		if parsed, err := strconv.Atoi(mc); err == nil && parsed > 0 {
			maxConns = parsed
		}
	}

	// 2. Inicialização do pool PostgreSQL (estritamente limitado a max_conns=5 para proteção do RDS)
	var repo repository.FamilyRepository
	var pool *pgxpool.Pool

	if dbURL != "" {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		cfg, err := pgxpool.ParseConfig(dbURL)
		if err != nil {
			cancel()
			log.Fatalf("[braga-chat] Erro ao interpretar DATABASE_URL: %v", err)
		}

		cfg.MaxConns = int32(maxConns)
		cfg.MinConns = 1
		cfg.MaxConnLifetime = 1 * time.Hour
		cfg.MaxConnIdleTime = 5 * time.Minute

		pool, err = pgxpool.NewWithConfig(ctx, cfg)
		cancel()
		if err != nil {
			log.Fatalf("[braga-chat] Falha ao conectar ao PostgreSQL: %v", err)
		}
		defer pool.Close()

		repo = repository.NewPostgresRepository(pool)
		log.Printf("[braga-chat] Pool PostgreSQL ativo (max_conns=%d)", maxConns)
	} else {
		log.Println("[braga-chat] AVISO: DATABASE_URL não informada. Executando em modo sem banco ativo.")
		repo = repository.NewPostgresRepository(nil)
	}

	// 2.5. Hub WebSocket em tempo real (SPEC-13 & D47)
	hub := transportws.NewHub()
	go hub.Run()

	// 3. Montagem das rotas e servidor HTTP
	router := transporthttp.NewRouter(repo, hub)
	server := &http.Server{
		Handler:      router,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 15 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	// 4. Transporte duplo: Socket Unix (/run/braga/chat.sock) ou Porta TCP (:8082)
	var listener net.Listener
	var err error

	if socketPath != "" {
		if err := os.MkdirAll(filepath.Dir(socketPath), 0755); err != nil {
			log.Fatalf("[braga-chat] Falha ao criar diretório do socket unix: %v", err)
		}
		_ = os.Remove(socketPath)

		listener, err = net.Listen("unix", socketPath)
		if err != nil {
			log.Fatalf("[braga-chat] Falha ao abrir listener Unix em %s: %v", socketPath, err)
		}
		if err := os.Chmod(socketPath, 0660); err != nil {
			log.Printf("[braga-chat] Aviso: falha ao alterar permissões do socket: %v", err)
		}
		defer os.Remove(socketPath)
		log.Printf("[braga-chat] Escutando em Socket Unix: %s (0660)", socketPath)
	} else {
		listener, err = net.Listen("tcp", listenAddr)
		if err != nil {
			log.Fatalf("[braga-chat] Falha ao abrir listener TCP em %s: %v", listenAddr, err)
		}
		log.Printf("[braga-chat] Escutando em Porta TCP: %s", listenAddr)
	}

	// 5. Execução em background
	go func() {
		if err := server.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("[braga-chat] Erro fatal no servidor HTTP: %v", err)
		}
	}()

	// 6. Graceful Shutdown suave ao receber SIGINT ou SIGTERM
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, os.Interrupt, syscall.SIGTERM)
	<-sigChan

	log.Println("[braga-chat] Sinal de interrupção recebido, iniciando encerramento gracioso...")
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutdownCancel()

	if err := server.Shutdown(shutdownCtx); err != nil {
		log.Printf("[braga-chat] Erro ao encerrar servidor HTTP: %v", err)
	}
	if err := hub.Shutdown(shutdownCtx); err != nil {
		log.Printf("[braga-chat] Erro ao encerrar conexões WebSocket: %v", err)
	}
	log.Println("[braga-chat] Processo encerrado com sucesso.")
}

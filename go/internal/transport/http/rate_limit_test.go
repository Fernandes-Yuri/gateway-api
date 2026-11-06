package http

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestSlidingWindowLimiter(t *testing.T) {
	now := time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)
	limiter := newSlidingWindowLimiter(2, time.Minute)
	limiter.now = func() time.Time { return now }

	key := "203.0.113.10"
	if !limiter.allow(key) || !limiter.allow(key) {
		t.Fatalf("As duas primeiras requisições deveriam ser permitidas")
	}
	if limiter.allow(key) {
		t.Fatalf("A terceira requisição deveria ser bloqueada")
	}

	// Janela deslizante: após 1 minuto as entradas antigas expiram.
	now = now.Add(61 * time.Second)
	if !limiter.allow(key) {
		t.Fatalf("Após a janela a requisição deveria ser permitida novamente")
	}

	// Chaves distintas têm orçamentos independentes.
	if !limiter.allow("198.51.100.7") {
		t.Fatalf("Outra chave deveria ter orçamento próprio")
	}
}

func TestClientIPFromRequest(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/sync/family-binding", nil)
	req.Header.Set("cf-connecting-ip", "203.0.113.42")
	if got := clientIPFromRequest(req); got != "203.0.113.42" {
		t.Fatalf("Esperava HEADER 203.0.113.42, obteve %q", got)
	}

	req = httptest.NewRequest(http.MethodPost, "/api/sync/family-binding", nil)
	req.RemoteAddr = "192.0.2.55:4321"
	if got := clientIPFromRequest(req); got != "192.0.2.55" {
		t.Fatalf("Esperava RemoteAddr 192.0.2.55, obteve %q", got)
	}
}

func TestRateLimitMiddlewareReturns429(t *testing.T) {
	limiter := newSlidingWindowLimiter(1, time.Minute)
	handler := rateLimitMiddleware(limiter)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	first := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/sync/family-binding", nil)
	req.RemoteAddr = "192.0.2.9:1111"
	handler.ServeHTTP(first, req)
	if first.Code != http.StatusOK {
		t.Fatalf("Primeira requisição deveria ser 200, obteve %d", first.Code)
	}

	second := httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodPost, "/api/sync/family-binding", nil)
	req.RemoteAddr = "192.0.2.9:2222"
	handler.ServeHTTP(second, req)
	if second.Code != http.StatusTooManyRequests {
		t.Fatalf("Segunda requisição deveria ser 429, obteve %d", second.Code)
	}
}

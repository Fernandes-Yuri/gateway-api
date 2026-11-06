package http

import (
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

// slidingWindowLimiter limita requisições por chave (IP) numa janela deslizante.
// Fase #7: o rate limit por rota do Python legado (10/min em
// /api/sync/family-binding) não era coberto pelo limiter global da borda — o
// braga-edge aplica firewall/bloqueio de IP, mas não janelas por rota.
type slidingWindowLimiter struct {
	mu     sync.Mutex
	hits   map[string][]time.Time
	limit  int
	window time.Duration
	now    func() time.Time
}

func newSlidingWindowLimiter(limit int, window time.Duration) *slidingWindowLimiter {
	return &slidingWindowLimiter{
		hits:   make(map[string][]time.Time),
		limit:  limit,
		window: window,
		now:    time.Now,
	}
}

func (l *slidingWindowLimiter) allow(key string) bool {
	now := l.now()
	cutoff := now.Add(-l.window)

	l.mu.Lock()
	defer l.mu.Unlock()

	entries := l.hits[key]
	kept := entries[:0]
	for _, timestamp := range entries {
		if timestamp.After(cutoff) {
			kept = append(kept, timestamp)
		}
	}
	if len(kept) >= l.limit {
		l.hits[key] = kept
		return false
	}
	l.hits[key] = append(kept, now)
	return true
}

// clientIPFromRequest usa o IP confiável do Cloudflare quando presente; o
// braga-edge é o único cliente do serviço, então o header é confiável aqui.
func clientIPFromRequest(r *http.Request) string {
	if ip := strings.TrimSpace(r.Header.Get("cf-connecting-ip")); ip != "" {
		return ip
	}
	host, _, err := net.SplitHostPort(strings.TrimSpace(r.RemoteAddr))
	if err == nil && host != "" {
		return host
	}
	if remote := strings.TrimSpace(r.RemoteAddr); remote != "" {
		return remote
	}
	return "unknown"
}

// rateLimitMiddleware responde 429 quando a chave (IP) excede a janela.
func rateLimitMiddleware(limiter *slidingWindowLimiter) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if !limiter.allow(clientIPFromRequest(r)) {
				writeJSONError(w, http.StatusTooManyRequests, "Muitas tentativas. Aguarde um instante.")
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

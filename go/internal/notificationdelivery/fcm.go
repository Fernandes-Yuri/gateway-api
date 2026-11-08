package notificationdelivery

import (
	"bytes"
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

const (
	defaultTokenURI = "https://oauth2.googleapis.com/token"
	defaultFCMBase  = "https://fcm.googleapis.com"
	fcmScope        = "https://www.googleapis.com/auth/firebase.messaging"
)

type ServiceAccountConfig struct {
	ProjectID   string `json:"project_id"`
	ClientEmail string `json:"client_email"`
	PrivateKey  string `json:"private_key"`
	TokenURI    string `json:"token_uri"`
	FCMBaseURL  string `json:"-"`
}

type HTTPv1Sender struct {
	projectID   string
	clientEmail string
	privateKey  *rsa.PrivateKey
	tokenURI    string
	fcmBaseURL  string
	httpClient  *http.Client

	mu          sync.Mutex
	accessToken string
	tokenExpiry time.Time
}

// NewConfiguredSender lê a credencial da conta de serviço do Firebase a partir
// do ambiente (FIREBASE_SERVICE_ACCOUNT_JSON, FIREBASE_SERVICE_ACCOUNT_PATH ou
// GOOGLE_APPLICATION_CREDENTIALS) sem expor segredos em logs.
func NewConfiguredSender(ctx context.Context) (PushSender, error) {
	_ = ctx
	rawJSON := strings.TrimSpace(os.Getenv("FIREBASE_SERVICE_ACCOUNT_JSON"))
	if rawJSON == "" {
		path := strings.TrimSpace(os.Getenv("FIREBASE_SERVICE_ACCOUNT_PATH"))
		if path == "" {
			path = strings.TrimSpace(os.Getenv("GOOGLE_APPLICATION_CREDENTIALS"))
		}
		if path == "" {
			return nil, errors.New("fcm service account not configured")
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, errors.New("fcm service account unreadable")
		}
		rawJSON = string(data)
	}
	var cfg ServiceAccountConfig
	if err := json.Unmarshal([]byte(rawJSON), &cfg); err != nil {
		return nil, errors.New("fcm service account invalid json")
	}
	return NewHTTPv1Sender(cfg, nil)
}

func NewHTTPv1Sender(cfg ServiceAccountConfig, client *http.Client) (*HTTPv1Sender, error) {
	projectID := strings.TrimSpace(cfg.ProjectID)
	clientEmail := strings.TrimSpace(cfg.ClientEmail)
	if projectID == "" || clientEmail == "" || strings.TrimSpace(cfg.PrivateKey) == "" {
		return nil, errors.New("fcm service account missing required fields")
	}
	privKey, err := parseRSAPrivateKey(cfg.PrivateKey)
	if err != nil {
		return nil, err
	}
	tokenURI := strings.TrimSpace(cfg.TokenURI)
	if tokenURI == "" {
		tokenURI = defaultTokenURI
	}
	fcmBase := strings.TrimRight(strings.TrimSpace(cfg.FCMBaseURL), "/")
	if fcmBase == "" {
		fcmBase = defaultFCMBase
	}
	if client == nil {
		client = &http.Client{Timeout: 20 * time.Second}
	}
	return &HTTPv1Sender{
		projectID:   projectID,
		clientEmail: clientEmail,
		privateKey:  privKey,
		tokenURI:    tokenURI,
		fcmBaseURL:  fcmBase,
		httpClient:  client,
	}, nil
}

func parseRSAPrivateKey(pemText string) (*rsa.PrivateKey, error) {
	block, _ := pem.Decode([]byte(pemText))
	if block == nil {
		return nil, errors.New("invalid pem private key")
	}
	if key, err := x509.ParsePKCS8PrivateKey(block.Bytes); err == nil {
		if rsaKey, ok := key.(*rsa.PrivateKey); ok {
			return rsaKey, nil
		}
		return nil, errors.New("private key is not rsa")
	}
	if rsaKey, err := x509.ParsePKCS1PrivateKey(block.Bytes); err == nil {
		return rsaKey, nil
	}
	return nil, errors.New("unable to parse rsa private key")
}

func (s *HTTPv1Sender) Send(ctx context.Context, msg PushMessage) error {
	if strings.TrimSpace(msg.Token) == "" {
		return errors.New("empty push token")
	}
	bearer, err := s.bearerToken(ctx)
	if err != nil {
		return err
	}
	payload := map[string]any{
		"message": map[string]any{
			"token": msg.Token,
			"data":  msg.Data,
			"android": map[string]any{
				"priority": "HIGH",
			},
		},
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	endpoint := fmt.Sprintf("%s/v1/projects/%s/messages:send", s.fcmBaseURL, url.PathEscape(s.projectID))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	req.Header.Set("Content-Type", "application/json")
	resp, err := s.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("fcm status %d", resp.StatusCode)
	}
	return nil
}

func (s *HTTPv1Sender) bearerToken(ctx context.Context) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now().UTC()
	if s.accessToken != "" && now.Add(time.Minute).Before(s.tokenExpiry) {
		return s.accessToken, nil
	}
	assertion, err := s.signJWT(now)
	if err != nil {
		return "", err
	}
	form := url.Values{}
	form.Set("grant_type", "urn:ietf:params:oauth:grant-type:jwt-bearer")
	form.Set("assertion", assertion)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.tokenURI, strings.NewReader(form.Encode()))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := s.httpClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		return "", fmt.Errorf("oauth token status %d", resp.StatusCode)
	}
	var tokenResp struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 65536)).Decode(&tokenResp); err != nil {
		return "", err
	}
	if strings.TrimSpace(tokenResp.AccessToken) == "" {
		return "", errors.New("empty oauth access token")
	}
	ttl := time.Duration(tokenResp.ExpiresIn) * time.Second
	if ttl <= 0 {
		ttl = 55 * time.Minute
	}
	s.accessToken = tokenResp.AccessToken
	s.tokenExpiry = now.Add(ttl)
	return s.accessToken, nil
}

func (s *HTTPv1Sender) signJWT(now time.Time) (string, error) {
	headerJSON, _ := json.Marshal(map[string]string{
		"alg": "RS256",
		"typ": "JWT",
	})
	claimsJSON, _ := json.Marshal(map[string]any{
		"iss":   s.clientEmail,
		"sub":   s.clientEmail,
		"scope": fcmScope,
		"aud":   s.tokenURI,
		"iat":   now.Unix(),
		"exp":   now.Add(time.Hour).Unix(),
	})
	unsigned := base64.RawURLEncoding.EncodeToString(headerJSON) + "." + base64.RawURLEncoding.EncodeToString(claimsJSON)
	digest := sha256.Sum256([]byte(unsigned))
	sig, err := rsa.SignPKCS1v15(rand.Reader, s.privateKey, crypto.SHA256, digest[:])
	if err != nil {
		return "", err
	}
	return unsigned + "." + base64.RawURLEncoding.EncodeToString(sig), nil
}

package notificationdelivery

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type fakePushSender struct {
	sent    []PushMessage
	failFor map[string]bool
}

func (f *fakePushSender) Send(_ context.Context, msg PushMessage) error {
	if f.failFor[msg.Token] {
		return errors.New("simulated push failure")
	}
	f.sent = append(f.sent, msg)
	return nil
}

func TestHTTPv1SenderCachesOAuthTokenAndFormatsAndroidDataMessage(t *testing.T) {
	privKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	pkcs8Bytes, err := x509.MarshalPKCS8PrivateKey(privKey)
	if err != nil {
		t.Fatal(err)
	}
	pemBlock := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: pkcs8Bytes})

	var tokenCalls int32
	var sendCalls int32
	var receivedBody map[string]any

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/token":
			atomic.AddInt32(&tokenCalls, 1)
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"access_token":"cached-token-123","expires_in":3600}`))
		case "/v1/projects/braga-test/messages:send":
			atomic.AddInt32(&sendCalls, 1)
			if got := r.Header.Get("Authorization"); got != "Bearer cached-token-123" {
				t.Errorf("Authorization inesperado: %s", got)
			}
			_ = json.NewDecoder(r.Body).Decode(&receivedBody)
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"name":"projects/braga-test/messages/1"}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	sender, err := NewHTTPv1Sender(ServiceAccountConfig{
		ProjectID:   "braga-test",
		ClientEmail: "firebase-adminsdk@braga-test.iam.gserviceaccount.com",
		PrivateKey:  string(pemBlock),
		TokenURI:    srv.URL + "/token",
		FCMBaseURL:  srv.URL,
	}, srv.Client())
	if err != nil {
		t.Fatal(err)
	}

	ctx := context.Background()
	for i := range 2 {
		err := sender.Send(ctx, PushMessage{
			Token: "dev-token-1",
			Title: "Nova mensagem familiar",
			Body:  "Abra o Braga Saúde para consultar os detalhes.",
			Data: map[string]string{
				"type":        "FAMILY_MESSAGE",
				"recipientId": "patient",
			},
		})
		if err != nil {
			t.Fatalf("envio %d falhou: %v", i, err)
		}
	}

	if got := atomic.LoadInt32(&tokenCalls); got != 1 {
		t.Fatalf("tokenCalls=%d; esperado 1 (cache ativo)", got)
	}
	if got := atomic.LoadInt32(&sendCalls); got != 2 {
		t.Fatalf("sendCalls=%d; esperado 2", got)
	}
	msgMap, _ := receivedBody["message"].(map[string]any)
	if msgMap["token"] != "dev-token-1" {
		t.Fatalf("token inesperado no payload FCM: %v", msgMap["token"])
	}
}

func outboxTestPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	dsn := os.Getenv("TEST_POSTGRES_URL")
	if dsn == "" {
		t.Skip("PostgreSQL efêmero exclusivo do CI")
	}
	parsed, err := url.Parse(dsn)
	if err != nil || (parsed.Hostname() != "localhost" && parsed.Hostname() != "127.0.0.1") || parsed.Path != "/braga_privacy_test" {
		t.Fatal("o teste aceita somente o banco local braga_privacy_test")
	}
	ctx := context.Background()
	admin, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	schema := "go_outbox_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	quoted := pgx.Identifier{schema}.Sanitize()
	if _, err := admin.Exec(ctx, `CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public`); err != nil {
		admin.Close(ctx)
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+quoted); err != nil {
		admin.Close(ctx)
		t.Fatal(err)
	}
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatal(err)
	}
	cfg.ConnConfig.RuntimeParams["search_path"] = schema + ",public"
	cfg.MaxConns = 4
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		pool.Close()
		_, _ = admin.Exec(ctx, "DROP SCHEMA "+quoted+" CASCADE")
		_ = admin.Close(ctx)
	})
	_, file, _, _ := runtime.Caller(0)
	root := filepath.Clean(filepath.Join(filepath.Dir(file), "../../.."))
	for _, rel := range []string{
		"scripts/docker/init_schema.sql",
		"migrations/018_device_push_tokens_and_audit_schemas.sql",
		"migrations/080_clinical_sync_integrity.sql",
		"migrations/082_notification_center.sql",
	} {
		sql, err := os.ReadFile(filepath.Join(root, rel))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := pool.Exec(ctx, string(sql)); err != nil {
			t.Fatalf("migração %s: %v", rel, err)
		}
	}
	return pool
}

func TestWorkerDeliversPerDeviceWithoutDuplicateAndSkipsReadItems(t *testing.T) {
	pool := outboxTestPool(t)
	ctx := context.Background()

	if _, err := pool.Exec(ctx, `INSERT INTO profiles(id,full_name) VALUES('patient','Paciente'),('caregiver','Cuidador')`); err != nil {
		t.Fatal(err)
	}
	cid := uuid.NewString()
	if _, err := pool.Exec(ctx, `INSERT INTO consultations(id,user_id,caregiver_id,title,scheduled_date,status,created_by)
		VALUES($1,'patient','caregiver','Cardiologia',now()+interval '2 days','ACCEPTED','caregiver')`, cid); err != nil {
		t.Fatal(err)
	}
	var itemID string
	if err := pool.QueryRow(ctx, `SELECT notification_emit(
		'patient','consultation:'||$1||':create','consultation:'||$1,'CONSULTATION_CREATED','AGENDA',
		'consultation',$1,'patient','caregiver',now(),NULL,'{"status":"ACCEPTED"}'::jsonb,true)::text`, cid).Scan(&itemID); err != nil {
		t.Fatal(err)
	}

	sender := &fakePushSender{failFor: map[string]bool{"tok-b": true}}
	worker := NewWorker(pool, sender)

	// 1) Sem dispositivo cadastrado: permanece PENDING com NO_DEVICE e attempts=0.
	processed, err := worker.ProcessOne(ctx)
	if err != nil || !processed {
		t.Fatalf("ProcessOne sem device: processed=%v err=%v", processed, err)
	}
	var state, lastErr string
	var attempts int
	if err := pool.QueryRow(ctx, `SELECT state,COALESCE(last_error,''),attempts FROM notification_outbox WHERE item_id=$1`, itemID).Scan(&state, &lastErr, &attempts); err != nil {
		t.Fatal(err)
	}
	if state != "PENDING" || lastErr != "NO_DEVICE" || attempts != 0 {
		t.Fatalf("estado sem device inesperado: state=%s lastErr=%s attempts=%d", state, lastErr, attempts)
	}

	// 2) Dois aparelhos registrados; tok-b falha na primeira tentativa.
	if _, err := pool.Exec(ctx, `INSERT INTO device_push_tokens(token,user_id,role,platform,is_active)
		VALUES('tok-a','patient','patient','fcm',true),('tok-b','patient','patient','fcm',true)`); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `UPDATE notification_outbox SET available_at=now()-interval '1 second' WHERE item_id=$1`, itemID); err != nil {
		t.Fatal(err)
	}
	if _, err := worker.ProcessOne(ctx); err != nil {
		t.Fatal(err)
	}
	if len(sender.sent) != 1 || sender.sent[0].Token != "tok-a" {
		t.Fatalf("esperado envio apenas para tok-a, obtido: %+v", sender.sent)
	}

	// 3) Na tentativa seguinte tok-b se recupera; tok-a não recebe duplicata.
	sender.failFor["tok-b"] = false
	if _, err := pool.Exec(ctx, `UPDATE notification_outbox SET available_at=now()-interval '1 second' WHERE item_id=$1`, itemID); err != nil {
		t.Fatal(err)
	}
	if _, err := worker.ProcessOne(ctx); err != nil {
		t.Fatal(err)
	}
	if len(sender.sent) != 2 || sender.sent[1].Token != "tok-b" {
		t.Fatalf("esperado segundo envio apenas para tok-b, obtido: %+v", sender.sent)
	}
	if err := pool.QueryRow(ctx, `SELECT state FROM notification_outbox WHERE item_id=$1`, itemID).Scan(&state); err != nil {
		t.Fatal(err)
	}
	if state != "DELIVERED" {
		t.Fatalf("state=%s; esperado DELIVERED", state)
	}

	// 4) Novo evento já lido antes do worker rodar deve ser marcado SKIPPED sem envio push.
	if _, err := pool.Exec(ctx, `SELECT notification_emit(
		'patient','consultation:'||$1||':cancel','consultation:'||$1,'CONSULTATION_CANCELLED','AGENDA',
		'consultation',$1,'patient','caregiver',now(),NULL,'{"status":"CANCELLED"}'::jsonb,true)`, cid); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `SELECT notification_mark('patient',$1::uuid,revision,false) FROM notification_items WHERE id=$1`, itemID); err != nil {
		t.Fatal(err)
	}
	if _, err := worker.ProcessOne(ctx); err != nil {
		t.Fatal(err)
	}
	if len(sender.sent) != 2 {
		t.Fatalf("evento já lido não deveria disparar push; total enviados=%d", len(sender.sent))
	}
	if err := pool.QueryRow(ctx, `SELECT state FROM notification_outbox WHERE event_key=$1`, "consultation:"+cid+":cancel").Scan(&state); err != nil {
		t.Fatal(err)
	}
	if state != "SKIPPED" {
		t.Fatalf("state=%s; esperado SKIPPED para item já lido", state)
	}
}

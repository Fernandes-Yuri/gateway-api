package repository

import (
	"context"
	"errors"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/Fernandes-Yuri/gateway-api/go/internal/domain"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Aceita exclusivamente o PostgreSQL efêmero do Actions, nunca contas reais.
func notificationTestRepository(t *testing.T) (*PostgresRepository, *pgxpool.Pool) {
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
		t.Fatal("falha ao abrir banco efêmero")
	}
	schema := "go_notifications_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	quoted := pgx.Identifier{schema}.Sanitize()
	if _, err := admin.Exec(ctx, `CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public`); err != nil {
		admin.Close(ctx)
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+quoted); err != nil {
		admin.Close(ctx)
		t.Fatal(err)
	}
	config, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatal(err)
	}
	config.ConnConfig.RuntimeParams["search_path"] = schema + ",public"
	config.MaxConns = 4
	pool, err := pgxpool.NewWithConfig(ctx, config)
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
	for _, path := range []string{"scripts/docker/init_schema.sql", "migrations/017_care_os_global_contracts.sql",
		"migrations/079_family_messages_e2ee.sql", "migrations/082_notification_center.sql"} {
		sql, err := os.ReadFile(filepath.Join(root, path))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := pool.Exec(ctx, string(sql)); err != nil {
			t.Fatalf("migração %s: %v", path, err)
		}
	}
	return NewPostgresRepository(pool), pool
}

func notificationProfiles(t *testing.T, pool *pgxpool.Pool, users ...string) {
	t.Helper()
	for _, user := range users {
		if _, err := pool.Exec(context.Background(), "INSERT INTO profiles(id,full_name) VALUES($1,'Fixture')", user); err != nil {
			t.Fatal(err)
		}
	}
}

func notificationBinding(t *testing.T, pool *pgxpool.Pool, patient, caregiver, status string) string {
	t.Helper()
	id := uuid.NewString()
	var caregiverID *string
	if caregiver != "" {
		caregiverID = &caregiver
	}
	_, err := pool.Exec(context.Background(), `INSERT INTO family_bindings
		(id,patient_id,caregiver_id,caregiver_name,caregiver_relation,connection_code,status,expires_at)
		VALUES($1,$2,$3,'Fixture','Familiar',$4,$5,now()+interval '7 days')`,
		id, patient, caregiverID, strings.ToUpper(id[:8]), status)
	if err != nil {
		t.Fatal(err)
	}
	return id
}

func notificationMessage(patient, sender string) domain.FamilyMessage {
	now := time.Now().UTC().Truncate(time.Millisecond)
	return domain.FamilyMessage{ID: uuid.NewString(), PatientID: patient, SenderID: sender,
		SenderName: "Fixture", MessageText: "Conteúdo privado que não pertence à caixa", IconType: "LOVE",
		SentAt: now, ExpiresAt: now.Add(24 * time.Hour)}
}

func notificationCount(t *testing.T, pool *pgxpool.Pool, query string, args ...any) int {
	t.Helper()
	var count int
	if err := pool.QueryRow(context.Background(), query, args...).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func TestPostgresMessageEventsAreGroupedPrivateAndIdempotent(t *testing.T) {
	repo, pool := notificationTestRepository(t)
	ctx := context.Background()
	notificationProfiles(t, pool, "patient", "sender", "other", "revoked", "stranger")
	notificationBinding(t, pool, "patient", "sender", "ACTIVE")
	notificationBinding(t, pool, "patient", "other", "ACTIVE")
	notificationBinding(t, pool, "patient", "other", "ACTIVE")
	notificationBinding(t, pool, "patient", "revoked", "REVOKED")
	msg := notificationMessage("patient", "sender")
	msg.ID = strings.ToUpper(msg.ID)
	envelope := `{"v":1,"iv":"opaque-iv","ciphertext":"opaque-ciphertext"}`
	msg.BodyEncrypted, msg.EncryptionVersion = &envelope, 1
	for range 3 {
		if err := repo.SaveMessage(ctx, &msg); err != nil {
			t.Fatal(err)
		}
	}
	if got := notificationCount(t, pool, "SELECT count(*) FROM family_messages"); got != 1 {
		t.Fatalf("mensagens=%d; esperado 1", got)
	}
	if got := notificationCount(t, pool, "SELECT count(*) FROM notification_outbox"); got != 2 {
		t.Fatalf("outbox=%d; esperado paciente e outro cuidador uma vez", got)
	}
	if got := notificationCount(t, pool, `SELECT count(*) FROM notification_items
		WHERE recipient_id IN ('sender','revoked','stranger')`); got != 0 {
		t.Fatalf("destinatários indevidos=%d", got)
	}
	if got := notificationCount(t, pool, `SELECT count(*) FROM notification_sources
		WHERE payload<>'{}'::jsonb OR expires_at<>$1 OR resource_type<>'chat' OR resource_id<>'patient'`, msg.ExpiresAt); got != 0 {
		t.Fatalf("fontes com conteúdo/TTL/destino incorreto=%d", got)
	}
	second := notificationMessage("patient", "sender")
	if err := repo.SaveMessage(ctx, &second); err != nil {
		t.Fatal(err)
	}
	if got := notificationCount(t, pool, `SELECT count(*) FROM notification_items
		WHERE group_key='chat:patient' AND (snapshot->>'count')::int=2 AND (snapshot->>'unreadCount')::int=2`); got != 2 {
		t.Fatalf("grupos de mensagens=%d; esperado 2", got)
	}
}

func TestPostgresMessageRollsBackWhenOutboxCannotBePersisted(t *testing.T) {
	repo, pool := notificationTestRepository(t)
	ctx := context.Background()
	notificationProfiles(t, pool, "patient", "caregiver")
	notificationBinding(t, pool, "patient", "caregiver", "ACTIVE")
	_, err := pool.Exec(ctx, `CREATE FUNCTION reject_notification_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
		BEGIN RAISE EXCEPTION 'fixture outbox unavailable'; END $$;
		CREATE TRIGGER reject_notification_outbox BEFORE INSERT ON notification_outbox
		FOR EACH ROW EXECUTE FUNCTION reject_notification_outbox()`)
	if err != nil {
		t.Fatal(err)
	}
	msg := notificationMessage("patient", "patient")
	if err := repo.SaveMessage(ctx, &msg); err == nil {
		t.Fatal("falha da outbox deve impedir commit da mensagem")
	}
	for _, table := range []string{"family_messages", "notification_items", "notification_sources", "notification_outbox"} {
		if got := notificationCount(t, pool, "SELECT count(*) FROM "+table); got != 0 {
			t.Fatalf("%s teve commit parcial: %d", table, got)
		}
	}
}

func TestPostgresMessageReadReconcilesOnlyExplicitIDsForCurrentReader(t *testing.T) {
	repo, pool := notificationTestRepository(t)
	ctx := context.Background()
	notificationProfiles(t, pool, "patient", "reader1", "reader2")
	notificationBinding(t, pool, "patient", "reader1", "ACTIVE")
	notificationBinding(t, pool, "patient", "reader2", "ACTIVE")
	first, second := notificationMessage("patient", "patient"), notificationMessage("patient", "patient")
	for _, msg := range []*domain.FamilyMessage{&first, &second} {
		if err := repo.SaveMessage(ctx, msg); err != nil {
			t.Fatal(err)
		}
	}
	readCount := func(reader, id string) int {
		return notificationCount(t, pool, `SELECT count(*) FROM notification_sources s JOIN notification_items i ON i.id=s.item_id
			WHERE i.recipient_id=$1 AND s.event_key=$2 AND s.read_at IS NOT NULL`, reader, "message:"+id)
	}
	if err := repo.MarkMessagesAsRead(ctx, "patient", "reader1", []string{strings.ToUpper(first.ID)}); err != nil {
		t.Fatal(err)
	}
	if readCount("reader1", first.ID) != 1 || readCount("reader2", first.ID) != 0 || readCount("reader1", second.ID) != 0 {
		t.Fatal("leitura deve afetar somente o destinatário e ID confirmados")
	}
	// O is_read global já está true; a segunda conta ainda precisa reconciliar sua própria caixa.
	if err := repo.MarkMessagesAsRead(ctx, "patient", "reader2", []string{first.ID}); err != nil {
		t.Fatal(err)
	}
	if readCount("reader2", first.ID) != 1 {
		t.Fatal("recibo global anterior não pode impedir a leitura por destinatário")
	}
	if _, err := pool.Exec(ctx, `SELECT notification_mark('reader1',id,revision,false)
		FROM notification_items WHERE recipient_id='reader1' AND group_key='chat:patient'`); err != nil {
		t.Fatal(err)
	}
	if got := notificationCount(t, pool, "SELECT count(*) FROM family_messages WHERE id=$1 AND is_read", second.ID); got != 0 {
		t.Fatal("ler central não pode forjar recibo de chat")
	}
	if err := repo.MarkMessagesAsRead(ctx, "patient", "reader2"); err != nil {
		t.Fatal(err)
	}
	if readCount("reader2", second.ID) != 0 {
		t.Fatal("recibo legado sem IDs não comprova conteúdo efetivamente exibido")
	}
}

func TestPostgresDeletionAndExpiryRemoveOnlyRelevantMessageSources(t *testing.T) {
	repo, pool := notificationTestRepository(t)
	ctx := context.Background()
	notificationProfiles(t, pool, "patient", "caregiver")
	notificationBinding(t, pool, "patient", "caregiver", "ACTIVE")
	first, second := notificationMessage("patient", "patient"), notificationMessage("patient", "patient")
	for _, msg := range []*domain.FamilyMessage{&first, &second} {
		if err := repo.SaveMessage(ctx, msg); err != nil {
			t.Fatal(err)
		}
	}
	if err := repo.DeleteMessage(ctx, strings.ToUpper(first.ID), "patient", nil, nil); err != nil {
		t.Fatal(err)
	}
	if got := notificationCount(t, pool, "SELECT count(*) FROM notification_sources WHERE active AND event_key=$1", "message:"+first.ID); got != 0 {
		t.Fatal("mensagem excluída continua ativa na caixa")
	}
	if got := notificationCount(t, pool, `SELECT count(*) FROM notification_items
		WHERE recipient_id='caregiver' AND (snapshot->>'count')::int=1`); got != 1 {
		t.Fatal("a exclusão não deve apagar outra mensagem do mesmo grupo")
	}
	if _, err := pool.Exec(ctx, "UPDATE family_messages SET expires_at=now()-interval '1 second' WHERE id=$1", second.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, "SELECT notification_prune('caregiver')"); err != nil {
		t.Fatal(err)
	}
	if got := notificationCount(t, pool, `SELECT count(*) FROM notification_items
		WHERE recipient_id='caregiver' AND snapshot->>'deleted'='true'`); got != 1 {
		t.Fatal("TTL deve retirar conteúdo de referência da central")
	}
}

func TestPostgresBindingAcceptanceNotifiesOnlyCounterpartyOnce(t *testing.T) {
	repo, pool := notificationTestRepository(t)
	ctx := context.Background()
	notificationProfiles(t, pool, "patient", "caregiver")
	id := notificationBinding(t, pool, "patient", "", "PENDING")
	for range 2 {
		if _, err := repo.AcceptInvitation(ctx, id[:8], "caregiver", "Fixture", "Filha", nil); err != nil {
			t.Fatal(err)
		}
	}
	if got := notificationCount(t, pool, `SELECT count(*) FROM notification_outbox
		WHERE recipient_id='patient' AND kind='FAMILY_BINDING_ACCEPTED'`); got != 1 {
		t.Fatalf("avisos de aceite=%d; esperado 1", got)
	}
	if got := notificationCount(t, pool, "SELECT count(*) FROM notification_items WHERE recipient_id='caregiver'"); got != 0 {
		t.Fatal("ator não deve receber aviso do próprio aceite")
	}
}

func TestPostgresBindingRevocationPreservesOtherActiveGrantAndDeniesFormerAccess(t *testing.T) {
	repo, pool := notificationTestRepository(t)
	ctx := context.Background()
	notificationProfiles(t, pool, "patient", "caregiver")
	first := notificationBinding(t, pool, "patient", "caregiver", "ACTIVE")
	second := notificationBinding(t, pool, "patient", "caregiver", "ACTIVE")
	msg := notificationMessage("patient", "patient")
	if err := repo.SaveMessage(ctx, &msg); err != nil {
		t.Fatal(err)
	}
	revoke := func(id string) {
		t.Helper()
		if _, err := repo.SyncBinding(ctx, &domain.SyncFamilyBindingRequest{ID: id, Status: "REVOKED"}, "patient"); err != nil {
			t.Fatal(err)
		}
	}
	revoke(first)
	if got := notificationCount(t, pool, "SELECT count(*) FROM notification_sources WHERE active AND kind='FAMILY_MESSAGE'"); got != 1 {
		t.Fatal("outro vínculo ACTIVE deve preservar o acesso à mensagem")
	}
	revoke(second)
	revoke(second)
	if got := notificationCount(t, pool, "SELECT count(*) FROM notification_sources WHERE active AND kind='FAMILY_MESSAGE'"); got != 0 {
		t.Fatal("revogação do último vínculo deve invalidar as mensagens na caixa")
	}
	if got := notificationCount(t, pool, "SELECT count(*) FROM notification_outbox WHERE kind='FAMILY_BINDING_REVOKED'"); got != 2 {
		t.Fatalf("avisos de revogação=%d; esperado um por vínculo", got)
	}
	unauthorized := notificationMessage("patient", "caregiver")
	if err := repo.SaveMessage(ctx, &unauthorized); !errors.Is(err, ErrForbidden) {
		t.Fatalf("envio após revogação: %v", err)
	}
	if err := repo.MarkMessagesAsRead(ctx, "patient", "caregiver", []string{msg.ID}); !errors.Is(err, ErrForbidden) {
		t.Fatalf("leitura após revogação: %v", err)
	}
}

func TestPostgresNotificationCenterPaginationDeltaAndMarkAllBound(t *testing.T) {
	repo, pool := notificationTestRepository(t)
	ctx := context.Background()
	notificationProfiles(t, pool, "patient", "caregiver", "stranger")
	b1 := notificationBinding(t, pool, "patient", "", "PENDING")
	if _, err := repo.AcceptInvitation(ctx, b1[:8], "caregiver", "Ana", "Filha", nil); err != nil {
		t.Fatal(err)
	}
	msg := notificationMessage("patient", "caregiver")
	if err := repo.SaveMessage(ctx, &msg); err != nil {
		t.Fatal(err)
	}

	// Paciente possui 2 grupos não lidos (vínculo aceito + chat:patient).
	unread, err := repo.NotificationUnread(ctx, "patient")
	if err != nil {
		t.Fatal(err)
	}
	if unread.UnreadCount != 2 || unread.LatestRevision <= 0 {
		t.Fatalf("unread=%+v; esperado UnreadCount=2", unread)
	}

	// Paginação de 1 em 1 sem perda nem duplicação, e rejeição de cursor adulterado ou de outra conta.
	firstPage, err := repo.ListNotifications(ctx, "patient", NotificationListOptions{Limit: 1})
	if err != nil {
		t.Fatal(err)
	}
	if len(firstPage.Items) != 1 || !firstPage.HasMore || firstPage.NextCursor == nil {
		t.Fatalf("firstPage inválida: %+v", firstPage)
	}
	if _, err := repo.ListNotifications(ctx, "caregiver", NotificationListOptions{Limit: 1, Cursor: *firstPage.NextCursor}); !errors.Is(err, ErrNotificationValidation) {
		t.Fatalf("cursor de outra conta deveria falhar com ErrNotificationValidation, obtido: %v", err)
	}
	secondPage, err := repo.ListNotifications(ctx, "patient", NotificationListOptions{Limit: 1, Cursor: *firstPage.NextCursor})
	if err != nil {
		t.Fatal(err)
	}
	if len(secondPage.Items) != 1 || secondPage.HasMore || secondPage.NextCursor != nil {
		t.Fatalf("secondPage inválida: %+v", secondPage)
	}

	// "Marcar todas como lidas" com revisão anterior preserva como não lida a mensagem que chegou depois.
	boundRev := firstPage.LatestRevision
	b2 := notificationBinding(t, pool, "patient", "", "PENDING")
	if _, err := repo.AcceptInvitation(ctx, b2[:8], "caregiver", "Ana", "Filha", nil); err != nil {
		t.Fatal(err)
	}
	afterMark, err := repo.MarkNotifications(ctx, "patient", "", boundRev, false)
	if err != nil {
		t.Fatal(err)
	}
	if afterMark.UnreadCount != 1 {
		t.Fatalf("evento posterior ao throughRevision deve permanecer não lido; UnreadCount=%d", afterMark.UnreadCount)
	}

	// Delta incremental a partir de 0 retorna alterações do destinatário.
	delta, err := repo.NotificationChanges(ctx, "patient", 0, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(delta.Changes) == 0 || delta.UnreadCount != 1 {
		t.Fatalf("delta inesperado: %+v", delta)
	}
}

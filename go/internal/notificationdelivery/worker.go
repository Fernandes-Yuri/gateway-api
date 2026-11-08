package notificationdelivery

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"log"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

const maxAttempts = 8

// O adaptador recebe dados genéricos; o worker nunca manipula credenciais.
type PushMessage struct {
	Token string
	Title string
	Body  string
	Data  map[string]string
}

type PushSender interface {
	Send(context.Context, PushMessage) error
}

type Worker struct {
	pool   *pgxpool.Pool
	sender PushSender
}

func NewWorker(pool *pgxpool.Pool, sender PushSender) *Worker {
	return &Worker{pool: pool, sender: sender}
}

type job struct {
	ID          int64
	ItemID      string
	RecipientID string
	EventKey    string
	Revision    int64
	Kind        string
	Attempts    int
	LeaseID     string
	State       string
}

type source struct {
	Kind         string
	PatientID    *string
	ResourceType string
	ResourceID   string
}

type device struct {
	token string
	hash  string
}

var errLeaseLost = errors.New("notification lease lost")

// ProcessOne reivindica somente um evento, para não deixar um lote esperando
// a sua vez enquanto os leases já estão correndo.
func (w *Worker) ProcessOne(ctx context.Context) (bool, error) {
	if w.pool == nil || w.sender == nil {
		return false, errors.New("notification delivery is not configured")
	}
	entry, err := w.claim(ctx)
	if err != nil || entry == nil {
		return false, err
	}
	if entry.State == "FAILED" {
		return true, nil
	}
	err = w.deliver(ctx, entry)
	if errors.Is(err, errLeaseLost) {
		return true, nil
	}
	if err != nil && ctx.Err() == nil {
		if finishErr := w.retry(ctx, entry, "DELIVERY_ERROR"); finishErr != nil {
			return true, finishErr
		}
	}
	return true, err
}

func (w *Worker) claim(ctx context.Context) (*job, error) {
	entry := &job{LeaseID: uuid.NewString()}
	err := w.pool.QueryRow(ctx, `WITH ready AS (
		SELECT id FROM notification_outbox
		WHERE (state='PENDING' AND available_at<=now())
		   OR (state='SENDING' AND lease_until<=now())
		ORDER BY available_at,id FOR UPDATE SKIP LOCKED LIMIT 1
	) UPDATE notification_outbox o SET
		state=CASE WHEN o.attempts>=$2 THEN 'FAILED' ELSE 'SENDING' END,
		attempts=CASE WHEN o.attempts>=$2 THEN o.attempts ELSE o.attempts+1 END,
		last_error=CASE WHEN o.attempts>=$2 THEN 'RETRY_EXHAUSTED' ELSE o.last_error END,
		lease_id=CASE WHEN o.attempts>=$2 THEN NULL ELSE $1::uuid END,
		lease_until=CASE WHEN o.attempts>=$2 THEN NULL ELSE now()+interval '5 minutes' END
		FROM ready WHERE o.id=ready.id
		RETURNING o.id,o.item_id::text,o.recipient_id,o.event_key,o.revision,o.kind,o.attempts,o.state`,
		entry.LeaseID, maxAttempts).Scan(&entry.ID, &entry.ItemID, &entry.RecipientID, &entry.EventKey,
		&entry.Revision, &entry.Kind, &entry.Attempts, &entry.State)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return entry, nil
}

// A fonte exata é revalidada, nunca substituída pelo último evento do grupo.
// Uma mensagem posterior lida/removida não pode suprimir uma anterior não lida.
func (w *Worker) currentSource(ctx context.Context, tx pgx.Tx, entry *job) (*source, error) {
	var alive bool
	err := tx.QueryRow(ctx, `SELECT true FROM notification_outbox
		WHERE id=$1 AND lease_id=$2 AND state='SENDING' AND lease_until>now()
		  AND item_id=$3 AND recipient_id=$4 FOR UPDATE`, entry.ID, entry.LeaseID, entry.ItemID, entry.RecipientID).Scan(&alive)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, errLeaseLost
	}
	if err != nil {
		return nil, err
	}
	if _, err := tx.Exec(ctx, "SELECT notification_prune($1)", entry.RecipientID); err != nil {
		return nil, err
	}
	current := &source{}
	err = tx.QueryRow(ctx, `SELECT s.kind,s.patient_id,s.resource_type,s.resource_id
		FROM notification_sources s JOIN notification_items i ON i.id=s.item_id
		JOIN profiles p ON p.id=i.recipient_id
		WHERE s.item_id=$1 AND s.event_key=$2 AND i.recipient_id=$3
		  AND s.active AND s.expires_at>now() AND s.read_at IS NULL AND s.archived_at IS NULL
		  AND s.event_revision<=$4 AND COALESCE(p.notifications_enabled,true)
		  AND notification_source_allowed(s.kind,i.recipient_id,s.resource_type,s.resource_id,s.patient_id,s.actor_id,s.event_key)`,
		entry.ItemID, entry.EventKey, entry.RecipientID, entry.Revision).Scan(
		&current.Kind, &current.PatientID, &current.ResourceType, &current.ResourceID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return current, err
}

func (w *Worker) load(ctx context.Context, entry *job) (*source, []device, bool, error) {
	tx, err := w.pool.Begin(ctx)
	if err != nil {
		return nil, nil, false, err
	}
	defer tx.Rollback(ctx)
	current, err := w.currentSource(ctx, tx, entry)
	if err != nil {
		return nil, nil, false, err
	}
	if current == nil {
		return nil, nil, false, tx.Commit(ctx)
	}
	rows, err := tx.Query(ctx, `SELECT token FROM device_push_tokens
		WHERE user_id=$1 AND is_active AND platform='fcm' ORDER BY token`, entry.RecipientID)
	if err != nil {
		return nil, nil, false, err
	}
	var devices []device
	for rows.Next() {
		var token string
		if err := rows.Scan(&token); err != nil {
			rows.Close()
			return nil, nil, false, err
		}
		digest := sha256.Sum256([]byte(token))
		devices = append(devices, device{token: token, hash: hex.EncodeToString(digest[:])})
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, nil, false, err
	}
	return current, devices, len(devices) > 0, tx.Commit(ctx)
}

// O lease, dono do token e autorização são checados novamente antes de CADA
// envio. O commit ocorre antes da chamada de rede, sem transação longa com FCM.
func (w *Worker) beforeSend(ctx context.Context, entry *job, target device) (*source, bool, error) {
	tx, err := w.pool.Begin(ctx)
	if err != nil {
		return nil, false, err
	}
	defer tx.Rollback(ctx)
	current, err := w.currentSource(ctx, tx, entry)
	if err != nil {
		return nil, false, err
	}
	if current == nil {
		return nil, false, tx.Commit(ctx)
	}
	var eligible bool
	err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM device_push_tokens
		WHERE token=$1 AND user_id=$2 AND is_active AND platform='fcm')
		AND NOT EXISTS(SELECT 1 FROM notification_push_deliveries WHERE outbox_id=$3 AND device_hash=$4)`,
		target.token, entry.RecipientID, entry.ID, target.hash).Scan(&eligible)
	if err != nil {
		return nil, false, err
	}
	if _, err := tx.Exec(ctx, `UPDATE notification_outbox SET lease_until=now()+interval '5 minutes'
		WHERE id=$1 AND lease_id=$2 AND state='SENDING' AND lease_until>now()`, entry.ID, entry.LeaseID); err != nil {
		return nil, false, err
	}
	return current, eligible, tx.Commit(ctx)
}

func (w *Worker) recordDelivered(ctx context.Context, entry *job, target device) error {
	result, err := w.pool.Exec(ctx, `INSERT INTO notification_push_deliveries(outbox_id,device_hash)
		SELECT id,$3 FROM notification_outbox WHERE id=$1 AND lease_id=$2 AND state='SENDING' AND lease_until>now()
		ON CONFLICT DO NOTHING`, entry.ID, entry.LeaseID, target.hash)
	if err != nil {
		return err
	}
	if result.RowsAffected() == 0 {
		var stillOwned bool
		if err := w.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM notification_outbox
			WHERE id=$1 AND lease_id=$2 AND state='SENDING' AND lease_until>now())`, entry.ID, entry.LeaseID).Scan(&stillOwned); err != nil {
			return err
		}
		if !stillOwned {
			return errLeaseLost
		}
	}
	return nil
}

func (w *Worker) deliver(ctx context.Context, entry *job) error {
	current, devices, hasDevice, err := w.load(ctx, entry)
	if err != nil {
		return err
	}
	if current == nil {
		return w.finish(ctx, entry, "SKIPPED", "", false)
	}
	if !hasDevice {
		return w.finish(ctx, entry, "PENDING", "NO_DEVICE", true)
	}
	failed := false
	for _, target := range devices {
		current, eligible, err := w.beforeSend(ctx, entry, target)
		if err != nil {
			return err
		}
		if current == nil {
			return w.finish(ctx, entry, "SKIPPED", "", false)
		}
		if !eligible {
			continue
		}
		sendCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
		err = w.sender.Send(sendCtx, pushMessage(entry, current, target.token))
		cancel()
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if err != nil {
			failed = true
			continue
		}
		if err := w.recordDelivered(ctx, entry, target); err != nil {
			return err
		}
	}
	if failed {
		return w.retry(ctx, entry, "PUSH_NOT_ACCEPTED")
	}
	// Os tokens podem ter mudado de dono durante o lote. Sem token atual,
	// aguarda novo registro em vez de consumir tentativas de envio.
	finalSource, _, hasDevice, err := w.load(ctx, entry)
	if err != nil {
		return err
	}
	if finalSource == nil {
		return w.finish(ctx, entry, "SKIPPED", "", false)
	}
	if !hasDevice {
		return w.finish(ctx, entry, "PENDING", "NO_DEVICE", true)
	}
	return w.finish(ctx, entry, "DELIVERED", "", false)
}

func retrySeconds(attempt int) int {
	shift := attempt - 1
	if shift < 0 {
		shift = 0
	}
	if shift > 8 {
		shift = 8
	}
	delay := 15 * (1 << shift)
	if delay > 3600 {
		return 3600
	}
	return delay
}

func (w *Worker) retry(ctx context.Context, entry *job, code string) error {
	state := "PENDING"
	if entry.Attempts >= maxAttempts {
		state = "FAILED"
	}
	return w.finish(ctx, entry, state, code, false)
}

func (w *Worker) finish(ctx context.Context, entry *job, state, code string, noDevice bool) error {
	delay := retrySeconds(entry.Attempts)
	if noDevice && delay < 300 {
		delay = 300
	}
	result, err := w.pool.Exec(ctx, `UPDATE notification_outbox SET state=$3,last_error=NULLIF($4,''),
		available_at=now()+make_interval(secs=>$5),lease_id=NULL,lease_until=NULL,
		attempts=attempts-CASE WHEN $6 THEN 1 ELSE 0 END
		WHERE id=$1 AND lease_id=$2 AND state='SENDING' AND lease_until>now()`,
		entry.ID, entry.LeaseID, state, code, delay, noDevice)
	if err != nil {
		return err
	}
	if result.RowsAffected() == 0 {
		return errLeaseLost
	}
	return nil
}

func pushMessage(entry *job, current *source, token string) PushMessage {
	title := "Aviso do Braga Saúde"
	switch {
	case current.Kind == "FAMILY_MESSAGE":
		title = "Nova mensagem familiar"
	case strings.HasPrefix(current.Kind, "CONSULTATION_"):
		title = "Atualização de consulta"
	case current.Kind == "POST_REACTION":
		title = "Nova interação no mural"
	}
	body := "Abra o Braga Saúde para consultar os detalhes."
	data := map[string]string{"type": current.Kind, "eventId": entry.ItemID, "revision": strconv.FormatInt(entry.Revision, 10),
		"recipientId": entry.RecipientID, "notificationContract": "1", "title": title, "message": body}
	if current.PatientID != nil {
		data["patientId"] = *current.PatientID
	}
	switch current.ResourceType {
	case "consultation":
		data["consultationId"] = current.ResourceID
	case "post":
		data["postId"] = current.ResourceID
	case "chat":
		data["sourceUserId"] = current.ResourceID
	case "medication":
		data["medicationId"] = current.ResourceID
	case "binding":
		data["bindingId"] = current.ResourceID
	}
	return PushMessage{Token: token, Title: title, Body: body, Data: data}
}

func (w *Worker) Run(ctx context.Context) {
	delay := 5 * time.Second
	for {
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
		processed, err := w.ProcessOne(ctx)
		if ctx.Err() != nil {
			return
		}
		if err != nil {
			// Nunca registra erro bruto de banco/transporte, tokens ou conteúdo.
			log.Print("[NotificationOutbox] DELIVERY_ERROR")
		}
		delay = 15 * time.Second
		if processed && err == nil {
			delay = time.Second
		}
	}
}

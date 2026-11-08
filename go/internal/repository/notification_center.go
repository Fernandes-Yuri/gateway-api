package repository

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

const NotificationContractVersion = 1

var ErrNotificationValidation = errors.New("parâmetros da central de notificações inválidos")

// NotificationRepository é opcional e independente do contrato do chat familiar.
// UID sempre é fornecido pela identidade autenticada, nunca pelo corpo da requisição.
type NotificationRepository interface {
	ListNotifications(context.Context, string, NotificationListOptions) (*NotificationPage, error)
	NotificationChanges(context.Context, string, int64, int) (*NotificationDelta, error)
	NotificationUnread(context.Context, string) (*NotificationState, error)
	MarkNotifications(context.Context, string, string, int64, bool) (*NotificationState, error)
}

type NotificationListOptions struct {
	Cursor          string
	Limit           int
	Category        string
	IncludeArchived bool
}

type NotificationPage struct {
	Contract         int               `json:"contract"`
	Items            []json.RawMessage `json:"items"`
	SnapshotRevision int64             `json:"snapshotRevision"`
	LatestRevision   int64             `json:"latestRevision"`
	UnreadCount      int64             `json:"unreadCount"`
	HasMore          bool              `json:"hasMore"`
	NextCursor       *string           `json:"nextCursor"`
}

type NotificationChange struct {
	Revision int64           `json:"revision"`
	Item     json.RawMessage `json:"item"`
}

type NotificationDelta struct {
	Contract       int                  `json:"contract"`
	Changes        []NotificationChange `json:"changes"`
	NextCursor     int64                `json:"nextCursor"`
	LatestRevision int64                `json:"latestRevision"`
	UnreadCount    int64                `json:"unreadCount"`
	HasMore        bool                 `json:"hasMore"`
}

type NotificationState struct {
	Contract       int   `json:"contract"`
	LatestRevision int64 `json:"latestRevision"`
	UnreadCount    int64 `json:"unreadCount"`
}

type notificationCursor struct {
	Version         int     `json:"v"`
	UID             string  `json:"u"`
	Category        *string `json:"c"`
	IncludeArchived *bool   `json:"a"`
	Snapshot        *int64  `json:"s"`
	Before          *int64  `json:"b"`
}

func notificationCategoryValid(category string) bool {
	switch category {
	case "", "FAMILY", "AGENDA", "COMMUNITY", "CARE":
		return true
	}
	return false
}

func encodeNotificationCursor(uid string, options NotificationListOptions, snapshot, before int64) string {
	frame := notificationCursor{NotificationContractVersion, uid, &options.Category, &options.IncludeArchived, &snapshot, &before}
	data, _ := json.Marshal(frame)
	return base64.RawURLEncoding.EncodeToString(data)
}

func decodeNotificationCursor(value, uid string, options NotificationListOptions) (int64, int64, error) {
	if len(value) == 0 || len(value) > 2048 {
		return 0, 0, ErrNotificationValidation
	}
	data, err := base64.RawURLEncoding.Strict().DecodeString(value)
	if err != nil {
		return 0, 0, ErrNotificationValidation
	}
	var frame notificationCursor
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&frame); err != nil {
		return 0, 0, ErrNotificationValidation
	}
	var trailing any
	if decoder.Decode(&trailing) != io.EOF || frame.Version != NotificationContractVersion || frame.UID != uid ||
		frame.Category == nil || frame.IncludeArchived == nil || frame.Snapshot == nil || frame.Before == nil ||
		*frame.Category != options.Category || *frame.IncludeArchived != options.IncludeArchived ||
		*frame.Snapshot < 0 || *frame.Before <= 0 || *frame.Before > *frame.Snapshot {
		return 0, 0, ErrNotificationValidation
	}
	return *frame.Snapshot, *frame.Before, nil
}

// O lock por destinatário precede a revisão e é compartilhado com os produtores
// Python/Go. Leitura, contador e alterações observam o mesmo estado autorizado.
func (r *PostgresRepository) notificationTransaction(ctx context.Context, uid string) (pgx.Tx, error) {
	if r == nil || r.pool == nil {
		return nil, ErrDatabaseUnavailable
	}
	if uid == "" {
		return nil, ErrForbidden
	}
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,8282))`, uid); err == nil {
		var paused bool
		err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM profiles p
			WHERE p.id=$1 AND to_jsonb(p)->>'paused_at' IS NOT NULL)`, uid).Scan(&paused)
		if err == nil && paused {
			err = ErrForbidden
		}
	}
	if err == nil {
		_, err = tx.Exec(ctx, `SELECT notification_prune($1)`, uid)
	}
	if err != nil {
		_ = tx.Rollback(ctx)
		return nil, err
	}
	return tx, nil
}

func notificationWatermark(ctx context.Context, tx pgx.Tx, uid string) (int64, error) {
	var current int64
	err := tx.QueryRow(ctx, `SELECT COALESCE(max(revision),0) FROM notification_changes WHERE recipient_id=$1`, uid).Scan(&current)
	return current, err
}

func notificationUnreadGroups(ctx context.Context, tx pgx.Tx, uid string) (int64, error) {
	var unread int64
	err := tx.QueryRow(ctx, `SELECT count(*) FROM notification_items WHERE recipient_id=$1
		AND snapshot->>'deleted'='false' AND snapshot->>'archived'='false'
		AND (snapshot->>'unreadCount')::integer>0`, uid).Scan(&unread)
	return unread, err
}

func (r *PostgresRepository) ListNotifications(ctx context.Context, uid string, options NotificationListOptions) (*NotificationPage, error) {
	if options.Limit < 1 || options.Limit > 100 || !notificationCategoryValid(options.Category) {
		return nil, ErrNotificationValidation
	}
	var snapshot int64
	var before *int64
	if options.Cursor != "" {
		selected, boundary, err := decodeNotificationCursor(options.Cursor, uid, options)
		if err != nil {
			return nil, err
		}
		snapshot, before = selected, &boundary
	}
	tx, err := r.notificationTransaction(ctx, uid)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	current, err := notificationWatermark(ctx, tx, uid)
	if err != nil {
		return nil, err
	}
	if before == nil {
		snapshot = current
	} else if snapshot > current {
		return nil, ErrNotificationValidation
	}
	var category any
	if options.Category != "" {
		category = options.Category
	}
	rows, err := tx.Query(ctx, `WITH latest AS (
		SELECT DISTINCT ON(item_id) item_id,revision,snapshot FROM notification_changes
		WHERE recipient_id=$1 AND revision<=$2 ORDER BY item_id,revision DESC
	) SELECT l.revision,i.snapshot FROM latest l JOIN notification_items i ON i.id=l.item_id
	  WHERE i.recipient_id=$1 AND ($3::bigint IS NULL OR l.revision<$3)
		AND l.snapshot->>'deleted'='false' AND i.snapshot->>'deleted'='false'
		AND ($4::text IS NULL OR (l.snapshot->>'category'=$4 AND i.snapshot->>'category'=$4))
		AND ($5 OR (l.snapshot->>'archived'='false' AND i.snapshot->>'archived'='false'))
	  ORDER BY l.revision DESC LIMIT $6`, uid, snapshot, before, category, options.IncludeArchived, options.Limit+1)
	if err != nil {
		return nil, err
	}
	items := make([]json.RawMessage, 0)
	revisions := make([]int64, 0)
	for rows.Next() {
		var revision int64
		var payload []byte
		if err := rows.Scan(&revision, &payload); err != nil {
			rows.Close()
			return nil, err
		}
		items = append(items, json.RawMessage(payload))
		revisions = append(revisions, revision)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	page := &NotificationPage{Contract: NotificationContractVersion, Items: items,
		SnapshotRevision: snapshot, LatestRevision: current, HasMore: len(items) > options.Limit}
	if page.HasMore {
		page.Items = items[:options.Limit]
		cursor := encodeNotificationCursor(uid, options, snapshot, revisions[options.Limit-1])
		page.NextCursor = &cursor
	}
	page.UnreadCount, err = notificationUnreadGroups(ctx, tx, uid)
	if err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return page, nil
}

func (r *PostgresRepository) NotificationChanges(ctx context.Context, uid string, after int64, limit int) (*NotificationDelta, error) {
	if after < 0 || limit < 1 || limit > 200 {
		return nil, ErrNotificationValidation
	}
	tx, err := r.notificationTransaction(ctx, uid)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	current, err := notificationWatermark(ctx, tx, uid)
	if err != nil {
		return nil, err
	}
	if after > current {
		return nil, ErrNotificationValidation
	}
	rows, err := tx.Query(ctx, `SELECT c.revision,i.snapshot FROM notification_changes c
		JOIN notification_items i ON i.id=c.item_id AND i.recipient_id=c.recipient_id
		WHERE c.recipient_id=$1 AND c.revision>$2 ORDER BY c.revision LIMIT $3`, uid, after, limit+1)
	if err != nil {
		return nil, err
	}
	changes := make([]NotificationChange, 0)
	for rows.Next() {
		var revision int64
		var payload []byte
		if err := rows.Scan(&revision, &payload); err != nil {
			rows.Close()
			return nil, err
		}
		changes = append(changes, NotificationChange{Revision: revision, Item: json.RawMessage(payload)})
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	delta := &NotificationDelta{Contract: NotificationContractVersion, Changes: changes,
		NextCursor: after, LatestRevision: current, HasMore: len(changes) > limit}
	if delta.HasMore {
		delta.Changes = changes[:limit]
	}
	if len(delta.Changes) > 0 {
		delta.NextCursor = delta.Changes[len(delta.Changes)-1].Revision
	}
	delta.UnreadCount, err = notificationUnreadGroups(ctx, tx, uid)
	if err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return delta, nil
}

func (r *PostgresRepository) NotificationUnread(ctx context.Context, uid string) (*NotificationState, error) {
	tx, err := r.notificationTransaction(ctx, uid)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	state, err := notificationCurrentState(ctx, tx, uid)
	if err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return state, nil
}

func notificationCurrentState(ctx context.Context, tx pgx.Tx, uid string) (*NotificationState, error) {
	current, err := notificationWatermark(ctx, tx, uid)
	if err != nil {
		return nil, err
	}
	unread, err := notificationUnreadGroups(ctx, tx, uid)
	if err != nil {
		return nil, err
	}
	return &NotificationState{Contract: NotificationContractVersion, LatestRevision: current, UnreadCount: unread}, nil
}

func (r *PostgresRepository) MarkNotifications(ctx context.Context, uid, itemID string, through int64, archive bool) (*NotificationState, error) {
	if through < 0 {
		return nil, ErrNotificationValidation
	}
	var item any
	if itemID != "" {
		parsed, err := uuid.Parse(itemID)
		if err != nil {
			return nil, ErrNotificationValidation
		}
		item = parsed.String()
	}
	tx, err := r.notificationTransaction(ctx, uid)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	current, err := notificationWatermark(ctx, tx, uid)
	if err != nil {
		return nil, err
	}
	if through > current {
		return nil, ErrNotificationValidation
	}
	if item != nil {
		var exists bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM notification_items WHERE id=$1 AND recipient_id=$2)`, item, uid).Scan(&exists); err != nil {
			return nil, err
		}
		if !exists {
			return nil, ErrNotFound
		}
	}
	if _, err := tx.Exec(ctx, `SELECT notification_mark($1,$2::uuid,$3,$4)`, uid, item, through, archive); err != nil {
		return nil, err
	}
	state, err := notificationCurrentState(ctx, tx, uid)
	if err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return state, nil
}

package repository

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/Fernandes-Yuri/gateway-api/go/internal/domain"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// PostgresRepository implementa FamilyRepository usando pgxpool
type PostgresRepository struct {
	pool *pgxpool.Pool
}

// NewPostgresRepository instancia o repositório PostgreSQL
func NewPostgresRepository(pool *pgxpool.Pool) *PostgresRepository {
	return &PostgresRepository{pool: pool}
}

// SaveMessage persiste a mensagem na tabela family_messages respeitando TTL e Blind Relay
func (r *PostgresRepository) SaveMessage(ctx context.Context, msg *domain.FamilyMessage) error {
	if r.pool == nil {
		return ErrDatabaseUnavailable
	}
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("falha ao iniciar transação de mensagem: %w", err)
	}
	defer tx.Rollback(ctx)
	allowed, err := r.checkAccessWithTx(ctx, tx, msg.SenderID, msg.PatientID)
	if err != nil {
		return err
	}
	if !allowed {
		return ErrForbidden
	}

	query := `
		INSERT INTO family_messages (
			id, patient_id, sender_id, sender_name, message_text,
			body_encrypted, encryption_version, icon_type, is_read,
			sent_at, expires_at
		) VALUES (
			$1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
		) ON CONFLICT DO NOTHING
		RETURNING id;
	`

	var returnedID string
	err = tx.QueryRow(
		ctx, query,
		msg.ID, msg.PatientID, msg.SenderID, msg.SenderName, msg.MessageText,
		msg.BodyEncrypted, msg.EncryptionVersion, msg.IconType, msg.IsRead,
		msg.SentAt, msg.ExpiresAt,
	).Scan(&returnedID)

	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			// Conflito de chave: idempotência se id, sender_id e patient_id coincidirem
			var exists bool
			checkErr := tx.QueryRow(
				ctx,
				`SELECT EXISTS(SELECT 1 FROM family_messages WHERE id=$1 AND sender_id=$2 AND patient_id=$3)`,
				msg.ID, msg.SenderID, msg.PatientID,
			).Scan(&exists)
			if checkErr == nil && exists {
				return tx.Commit(ctx)
			}
			return ErrConflict
		}
		return fmt.Errorf("falha ao salvar mensagem: %w", err)
	}

	if err := emitMessageNotifications(ctx, tx, msg); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// GetMessages busca mensagens não expiradas com limite, paginação e aplicação de tombstones
func (r *PostgresRepository) GetMessages(ctx context.Context, patientID string, limit, offset int) ([]domain.FamilyMessage, error) {
	if r.pool == nil {
		return nil, ErrDatabaseUnavailable
	}

	if limit <= 0 || limit > 200 {
		limit = 50
	}
	if offset < 0 {
		offset = 0
	}

	query := `
		SELECT id, patient_id, sender_id, sender_name, message_text,
		       body_encrypted, encryption_version, icon_type, is_read,
		       sent_at, expires_at, deleted_at
		FROM family_messages
		WHERE patient_id = $1 AND expires_at > now()
		ORDER BY sent_at DESC, id DESC
		LIMIT $2 OFFSET $3;
	`

	rows, err := r.pool.Query(ctx, query, patientID, limit, offset)
	if err != nil {
		return nil, fmt.Errorf("falha ao consultar mensagens: %w", err)
	}
	defer rows.Close()

	messages := make([]domain.FamilyMessage, 0)
	for rows.Next() {
		var m domain.FamilyMessage
		err := rows.Scan(
			&m.ID, &m.PatientID, &m.SenderID, &m.SenderName, &m.MessageText,
			&m.BodyEncrypted, &m.EncryptionVersion, &m.IconType, &m.IsRead,
			&m.SentAt, &m.ExpiresAt, &m.DeletedAt,
		)
		if err != nil {
			return nil, fmt.Errorf("falha ao escanear mensagem: %w", err)
		}

		// Regra de serialização/tombstone D47 + SPEC-13
		if m.DeletedAt != nil {
			m.MessageText = ""
			m.BodyEncrypted = nil
			m.SenderName = ""
			m.IconType = "LOVE"
		} else if m.BodyEncrypted != nil && *m.BodyEncrypted != "" {
			m.EncryptionVersion = domain.E2EECurrentVersion
			if m.MessageText == "" {
				m.MessageText = *m.BodyEncrypted
			}
		}

		messages = append(messages, m)
	}

	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("erro durante iteração de mensagens: %w", err)
	}

	return messages, nil
}

// DeleteMessage aplica tombstone se remetente for o proprietário (D47 / LGPD Art. 18)
func (r *PostgresRepository) DeleteMessage(ctx context.Context, messageID string, senderID string, patientID *string, sentAt *time.Time) error {
	if r.pool == nil {
		return ErrDatabaseUnavailable
	}

	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("falha ao iniciar transação: %w", err)
	}
	defer tx.Rollback(ctx)

	var currentSenderID string
	err = tx.QueryRow(ctx, `SELECT sender_id FROM family_messages WHERE id=$1 FOR UPDATE`, messageID).Scan(&currentSenderID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			if patientID == nil || sentAt == nil {
				// Idempotência quando linha já não existe
				return nil
			}

			// Valida acesso familiar
			allowed, checkErr := r.checkAccessWithTx(ctx, tx, senderID, *patientID)
			if checkErr != nil {
				return checkErr
			}
			if !allowed {
				return ErrForbidden
			}

			// Valida expiração
			now := time.Now().UTC()
			expires := sentAt.Add(24 * time.Hour)
			if !expires.After(now) {
				return nil
			}

			// Reserva o ID com tombstone imediato para evitar corrida de upload em voo
			insertQuery := `
				INSERT INTO family_messages (
					id, patient_id, sender_id, sender_name, message_text,
					body_encrypted, encryption_version, icon_type, is_read,
					sent_at, expires_at, deleted_at
				) VALUES (
					$1, $2, $3, '', '', NULL, 0, 'LOVE', false, $4, $5, now()
				) ON CONFLICT DO NOTHING;
			`
			if _, insertErr := tx.Exec(ctx, insertQuery, messageID, *patientID, senderID, *sentAt, expires); insertErr != nil {
				return fmt.Errorf("falha ao inserir reserva de tombstone: %w", insertErr)
			}

			err = tx.QueryRow(ctx, `SELECT sender_id FROM family_messages WHERE id=$1 FOR UPDATE`, messageID).Scan(&currentSenderID)
			if err != nil {
				return nil
			}
		} else {
			return fmt.Errorf("falha ao buscar mensagem para exclusão: %w", err)
		}
	}

	if currentSenderID != senderID {
		return ErrForbidden
	}

	updateQuery := `
		UPDATE family_messages
		SET deleted_at = COALESCE(deleted_at, now()),
		    message_text = '',
		    body_encrypted = NULL,
		    sender_name = '',
		    icon_type = 'LOVE'
		WHERE id = $1;
	`
	if _, err := tx.Exec(ctx, updateQuery, messageID); err != nil {
		return fmt.Errorf("falha ao atualizar tombstone da mensagem: %w", err)
	}
	if _, err := tx.Exec(ctx, `SELECT notification_remove_event($1)`, messageEventKey(messageID)); err != nil {
		return fmt.Errorf("falha ao remover aviso da mensagem excluída: %w", err)
	}

	return tx.Commit(ctx)
}

// MarkMessagesAsRead marca como lidas as mensagens do patientID que foram enviadas por outro usuário
func (r *PostgresRepository) MarkMessagesAsRead(ctx context.Context, patientID string, readerID string, messageIDs ...[]string) error {
	if r.pool == nil {
		return ErrDatabaseUnavailable
	}

	var ids []string
	if len(messageIDs) > 0 {
		ids = messageIDs[0]
	}
	if ids != nil {
		canonical := make([]string, len(ids))
		for i, id := range ids {
			canonical[i] = canonicalNotificationUUID(id)
		}
		ids = canonical
	}
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("falha ao iniciar transação de leitura: %w", err)
	}
	defer tx.Rollback(ctx)
	allowed, err := r.checkAccessWithTx(ctx, tx, readerID, patientID)
	if err != nil {
		return err
	}
	if !allowed {
		return ErrForbidden
	}
	query := `
		UPDATE family_messages
		SET is_read = true
		WHERE patient_id = $1 AND sender_id != $2 AND is_read = false
		  AND deleted_at IS NULL AND expires_at > now()
		  AND ($3::text[] IS NULL OR id::text = ANY($3::text[]));
	`
	if _, err := tx.Exec(ctx, query, patientID, readerID, ids); err != nil {
		return fmt.Errorf("falha ao marcar mensagens como lidas: %w", err)
	}
	// O legado sem IDs mantém o recibo antigo, mas não comprova quais mensagens
	// estiveram legíveis. A central só reconcilia IDs confirmados explicitamente.
	if ids != nil && len(ids) > 0 {
		rows, err := tx.Query(ctx, `
			SELECT id::text FROM family_messages
			WHERE patient_id=$1 AND sender_id<>$2 AND id::text=ANY($3::text[])
			  AND deleted_at IS NULL AND expires_at>now() ORDER BY id`, patientID, readerID, ids)
		if err != nil {
			return fmt.Errorf("falha ao consultar mensagens efetivamente lidas: %w", err)
		}
		var keys []string
		for rows.Next() {
			var id string
			if err := rows.Scan(&id); err != nil {
				rows.Close()
				return err
			}
			keys = append(keys, "message:"+id)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		if len(keys) > 0 {
			if _, err := tx.Exec(ctx, `SELECT notification_read_sources($1,$2::text[])`, readerID, keys); err != nil {
				return fmt.Errorf("falha ao reconciliar avisos da leitura: %w", err)
			}
		}
	}
	return tx.Commit(ctx)
}

// CheckFamilyAccess verifica se o usuário é o próprio titular ou cuidador com vínculo ativo
func (r *PostgresRepository) CheckFamilyAccess(ctx context.Context, uid string, patientID string) (bool, error) {
	if uid == patientID {
		return true, nil
	}
	if r.pool == nil {
		return false, ErrDatabaseUnavailable
	}

	var allowed bool
	query := `SELECT EXISTS(SELECT 1 FROM family_bindings WHERE patient_id=$1 AND caregiver_id=$2 AND status='ACTIVE');`
	err := r.pool.QueryRow(ctx, query, patientID, uid).Scan(&allowed)
	if err != nil {
		return false, fmt.Errorf("falha ao verificar acesso familiar: %w", err)
	}
	return allowed, nil
}

func (r *PostgresRepository) checkAccessWithTx(ctx context.Context, tx pgx.Tx, uid string, patientID string) (bool, error) {
	if uid == patientID {
		return true, nil
	}
	// Segura a concessão durante a operação. Uma revogação concorrente é
	// serializada com a mensagem/recibo, em vez de caber entre check e commit.
	var bindingID string
	query := `SELECT id::text FROM family_bindings
		WHERE patient_id=$1 AND caregiver_id=$2 AND status='ACTIVE'
		ORDER BY id LIMIT 1 FOR SHARE;`
	err := tx.QueryRow(ctx, query, patientID, uid).Scan(&bindingID)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("falha ao verificar acesso familiar na transação: %w", err)
	}
	return true, nil
}

// FindByConnectionCode localiza vínculo pendente por código de 8 caracteres
func (r *PostgresRepository) FindByConnectionCode(ctx context.Context, code string) (*domain.FamilyBinding, error) {
	cleanCode := strings.ToUpper(strings.TrimSpace(code))

	// Suporte ao código coringa especial de testes/demonstração TESTFAM1 (restrito a CI/testes)
	if cleanCode == domain.JokerConnectionCode && (os.Getenv("ALLOW_TEST_JOKER_CODE") == "true" || os.Getenv("CI") != "") {
		now := time.Now().UTC()
		jokerID := uuid.NewSHA1(uuid.NameSpaceDNS, []byte("testfam1-joker")).String()
		return &domain.FamilyBinding{
			ID:                 jokerID,
			PatientID:          domain.JokerPatientUID,
			PatientName:        domain.JokerPatientName,
			ConnectionCode:     domain.JokerConnectionCode,
			Status:             "PENDING",
			CaregiverName:      "",
			CaregiverRelation:  "",
			CaregiverRole:      "CAREGIVER_VIEWER",
			Permissions:        []string{"ALL"},
			PatientPublicKey:   nil,
			CaregiverPublicKey: nil,
			CreatedAt:          now,
			ExpiresAt:          now.Add(3650 * 24 * time.Hour),
		}, nil
	}

	if r.pool == nil {
		return nil, ErrDatabaseUnavailable
	}

	query := `
		SELECT b.id, b.patient_id, COALESCE(p.full_name, '') AS patient_name,
		       b.caregiver_id, b.caregiver_name, b.caregiver_relation,
		       b.connection_code, COALESCE(b.status, 'PENDING') AS status, b.caregiver_role, b.permissions,
		       b.patient_public_key, b.caregiver_public_key, b.created_at, b.expires_at
		FROM family_bindings b
		LEFT JOIN profiles p ON p.id = b.patient_id
		WHERE b.connection_code = $1 AND b.status = 'PENDING' AND b.expires_at > now();
	`

	var b domain.FamilyBinding
	err := r.pool.QueryRow(ctx, query, cleanCode).Scan(
		&b.ID, &b.PatientID, &b.PatientName,
		&b.CaregiverID, &b.CaregiverName, &b.CaregiverRelation,
		&b.ConnectionCode, &b.Status, &b.CaregiverRole, &b.Permissions,
		&b.PatientPublicKey, &b.CaregiverPublicKey, &b.CreatedAt, &b.ExpiresAt,
	)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, fmt.Errorf("falha ao buscar convite por código: %w", err)
	}

	b.Permissions = sanitizePermissions(b.Permissions)
	return &b, nil
}

// AcceptInvitation realiza o aceite de um vínculo familiar por código
func (r *PostgresRepository) AcceptInvitation(
	ctx context.Context,
	code string,
	caregiverID string,
	caregiverName string,
	caregiverRelation string,
	caregiverPublicKey *string,
) (*domain.FamilyBinding, error) {
	if r.pool == nil {
		return nil, ErrDatabaseUnavailable
	}

	cleanCode := strings.ToUpper(strings.TrimSpace(code))

	// Trata código coringa TESTFAM1 (restrito a CI/testes)
	if cleanCode == domain.JokerConnectionCode && (os.Getenv("ALLOW_TEST_JOKER_CODE") == "true" || os.Getenv("CI") != "") {
		if caregiverID == domain.JokerPatientUID {
			return nil, ErrConflict
		}

		tx, err := r.pool.Begin(ctx)
		if err != nil {
			return nil, fmt.Errorf("falha ao iniciar transação: %w", err)
		}
		defer tx.Rollback(ctx)

		// Garante perfil do paciente coringa
		_, err = tx.Exec(ctx, `
			INSERT INTO profiles(id, full_name, self_care_complete, role, created_at, updated_at)
			VALUES ($1, $2, true, 'PATIENT', now(), now())
			ON CONFLICT (id) DO UPDATE SET self_care_complete=true;
		`, domain.JokerPatientUID, domain.JokerPatientName)
		if err != nil {
			return nil, fmt.Errorf("falha ao garantir perfil do paciente coringa: %w", err)
		}

		// Garante perfil do cuidador
		_, err = tx.Exec(ctx, `INSERT INTO profiles(id, full_name) VALUES($1, $2) ON CONFLICT DO NOTHING;`, caregiverID, caregiverName)
		if err != nil {
			return nil, fmt.Errorf("falha ao registrar perfil cuidador: %w", err)
		}

		// Idempotência para vínculo ativo existente
		queryExisting := `
			SELECT b.id, b.patient_id, COALESCE(p.full_name, '') AS patient_name,
			       b.caregiver_id, b.caregiver_name, b.caregiver_relation,
			       b.connection_code, COALESCE(b.status, 'PENDING') AS status, b.caregiver_role, b.permissions,
			       b.patient_public_key, b.caregiver_public_key, b.created_at, b.expires_at
			FROM family_bindings b
			LEFT JOIN profiles p ON p.id = b.patient_id
			WHERE b.patient_id = $1 AND b.caregiver_id = $2 AND b.status = 'ACTIVE';
		`
		var existing domain.FamilyBinding
		err = tx.QueryRow(ctx, queryExisting, domain.JokerPatientUID, caregiverID).Scan(
			&existing.ID, &existing.PatientID, &existing.PatientName,
			&existing.CaregiverID, &existing.CaregiverName, &existing.CaregiverRelation,
			&existing.ConnectionCode, &existing.Status, &existing.CaregiverRole, &existing.Permissions,
			&existing.PatientPublicKey, &existing.CaregiverPublicKey, &existing.CreatedAt, &existing.ExpiresAt,
		)
		if err == nil {
			existing.Permissions = sanitizePermissions(existing.Permissions)
			if existing.PatientName == "" {
				existing.PatientName = domain.JokerPatientName
			}
			return &existing, tx.Commit(ctx)
		}

		// Cria vínculo ativo coringa
		now := time.Now().UTC()
		bindingID := uuid.New().String()
		insertJoker := `
			INSERT INTO family_bindings (
				id, patient_id, caregiver_id, caregiver_name, caregiver_relation,
				connection_code, status, created_at, expires_at, caregiver_role, permissions,
				caregiver_public_key
			) VALUES (
				$1, $2, $3, $4, $5, $6, 'ACTIVE', $7, $8, 'CAREGIVER_VIEWER', ARRAY['ALL']::text[], $9
			) RETURNING id, patient_id, caregiver_id, caregiver_name, caregiver_relation,
			            connection_code, status, created_at, expires_at, caregiver_role,
			            permissions, caregiver_public_key, patient_public_key;
		`
		var created domain.FamilyBinding
		err = tx.QueryRow(
			ctx, insertJoker,
			bindingID, domain.JokerPatientUID, caregiverID, caregiverName, caregiverRelation,
			domain.JokerConnectionCode, now, now.Add(3650*24*time.Hour), caregiverPublicKey,
		).Scan(
			&created.ID, &created.PatientID, &created.CaregiverID, &created.CaregiverName,
			&created.CaregiverRelation, &created.ConnectionCode, &created.Status, &created.CreatedAt,
			&created.ExpiresAt, &created.CaregiverRole, &created.Permissions, &created.CaregiverPublicKey,
			&created.PatientPublicKey,
		)
		if err != nil {
			return nil, fmt.Errorf("falha ao criar vínculo coringa: %w", err)
		}

		created.Permissions = sanitizePermissions(created.Permissions)
		created.PatientName = domain.JokerPatientName
		if err := emitBindingNotification(ctx, tx, &created, caregiverID); err != nil {
			return nil, err
		}
		return &created, tx.Commit(ctx)
	}

	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return nil, fmt.Errorf("falha ao iniciar transação: %w", err)
	}
	defer tx.Rollback(ctx)

	selectQuery := `
		SELECT id, patient_id, caregiver_id, caregiver_name, caregiver_relation,
		       connection_code, COALESCE(status, 'PENDING') AS status, created_at, expires_at, caregiver_role,
		       permissions, caregiver_public_key, patient_public_key
		FROM family_bindings
		WHERE connection_code = $1 FOR UPDATE;
	`
	var row domain.FamilyBinding
	err = tx.QueryRow(ctx, selectQuery, cleanCode).Scan(
		&row.ID, &row.PatientID, &row.CaregiverID, &row.CaregiverName,
		&row.CaregiverRelation, &row.ConnectionCode, &row.Status, &row.CreatedAt,
		&row.ExpiresAt, &row.CaregiverRole, &row.Permissions, &row.CaregiverPublicKey,
		&row.PatientPublicKey,
	)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, fmt.Errorf("falha ao buscar vínculo para aceite: %w", err)
	}
	row.Permissions = sanitizePermissions(row.Permissions)

	// Regras de validação de aceite (family_contract)
	if row.PatientID == caregiverID {
		return nil, ErrConflict
	}
	if row.Status == "ACTIVE" && row.CaregiverID != nil && *row.CaregiverID == caregiverID {
		return &row, nil
	}
	if row.Status != "PENDING" || row.CaregiverID != nil || row.ExpiresAt.Before(time.Now().UTC()) {
		return nil, ErrConflict
	}

	// Stub do perfil do cuidador
	_, err = tx.Exec(ctx, `INSERT INTO profiles(id, full_name) VALUES($1, $2) ON CONFLICT DO NOTHING;`, caregiverID, caregiverName)
	if err != nil {
		return nil, fmt.Errorf("falha ao registrar stub de perfil: %w", err)
	}

	updateQuery := `
		UPDATE family_bindings
		SET caregiver_id = $2,
		    caregiver_name = $3,
		    caregiver_relation = $4,
		    caregiver_public_key = COALESCE($5, caregiver_public_key),
		    status = 'ACTIVE'
		WHERE id = $1
		RETURNING id, patient_id, caregiver_id, caregiver_name, caregiver_relation,
		          connection_code, status, created_at, expires_at, caregiver_role,
		          permissions, caregiver_public_key, patient_public_key;
	`
	var updated domain.FamilyBinding
	err = tx.QueryRow(ctx, updateQuery, row.ID, caregiverID, caregiverName, caregiverRelation, caregiverPublicKey).Scan(
		&updated.ID, &updated.PatientID, &updated.CaregiverID, &updated.CaregiverName,
		&updated.CaregiverRelation, &updated.ConnectionCode, &updated.Status, &updated.CreatedAt,
		&updated.ExpiresAt, &updated.CaregiverRole, &updated.Permissions, &updated.CaregiverPublicKey,
		&updated.PatientPublicKey,
	)
	if err != nil {
		return nil, fmt.Errorf("falha ao atualizar vínculo familiar: %w", err)
	}

	updated.Permissions = sanitizePermissions(updated.Permissions)
	if err := emitBindingNotification(ctx, tx, &updated, caregiverID); err != nil {
		return nil, err
	}
	return &updated, tx.Commit(ctx)
}

// PublishPublicKey registra chave pública ECDH (SPEC-13 §2.1)
func (r *PostgresRepository) PublishPublicKey(
	ctx context.Context,
	bindingID *string,
	patientID *string,
	uid string,
	publicKey string,
) (*domain.PublishFamilyPublicKeyResponse, error) {
	if r.pool == nil {
		return nil, ErrDatabaseUnavailable
	}

	if bindingID != nil && *bindingID != "" {
		tx, err := r.pool.Begin(ctx)
		if err != nil {
			return nil, fmt.Errorf("falha ao iniciar transação: %w", err)
		}
		defer tx.Rollback(ctx)

		var pID string
		var cID *string
		query := `SELECT patient_id, caregiver_id FROM family_bindings WHERE id=$1 FOR UPDATE;`
		err = tx.QueryRow(ctx, query, *bindingID).Scan(&pID, &cID)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return nil, ErrForbidden
			}
			return nil, fmt.Errorf("falha ao verificar vínculo: %w", err)
		}

		if uid != pID && (cID == nil || uid != *cID) {
			return nil, ErrForbidden
		}

		if uid == pID {
			_, err = tx.Exec(ctx, `UPDATE family_bindings SET patient_public_key=$2 WHERE id=$1;`, *bindingID, publicKey)
		} else {
			_, err = tx.Exec(ctx, `UPDATE family_bindings SET caregiver_public_key=$2 WHERE id=$1;`, *bindingID, publicKey)
		}
		if err != nil {
			return nil, fmt.Errorf("falha ao gravar chave pública: %w", err)
		}

		if err := tx.Commit(ctx); err != nil {
			return nil, err
		}

		return &domain.PublishFamilyPublicKeyResponse{
			Status:    "success",
			PatientID: pID,
			PublicKey: publicKey,
		}, nil
	}

	if patientID != nil && *patientID != "" {
		allowed, err := r.CheckFamilyAccess(ctx, uid, *patientID)
		if err != nil {
			return nil, err
		}
		if !allowed {
			return nil, ErrForbidden
		}

		if uid == *patientID {
			_, err = r.pool.Exec(
				ctx,
				`UPDATE family_bindings SET patient_public_key=$2 WHERE patient_id=$1 AND status IN ('PENDING', 'ACTIVE');`,
				*patientID, publicKey,
			)
		} else {
			_, err = r.pool.Exec(
				ctx,
				`UPDATE family_bindings SET caregiver_public_key=$3 WHERE patient_id=$1 AND caregiver_id=$2 AND status='ACTIVE';`,
				*patientID, uid, publicKey,
			)
		}
		if err != nil {
			return nil, fmt.Errorf("falha ao atualizar chave por patientId: %w", err)
		}

		return &domain.PublishFamilyPublicKeyResponse{
			Status:    "success",
			PatientID: *patientID,
			PublicKey: publicKey,
		}, nil
	}

	return nil, errors.New("informe bindingId ou patientId")
}

// ListBindings lista vínculos ativos e pendentes do usuário titular ou cuidador
func (r *PostgresRepository) ListBindings(ctx context.Context, userID string) ([]domain.FamilyBinding, error) {
	if r.pool == nil {
		return nil, ErrDatabaseUnavailable
	}

	// Atualiza convites pendentes vencidos
	_, _ = r.pool.Exec(
		ctx,
		`UPDATE family_bindings SET status='EXPIRED' WHERE status='PENDING' AND expires_at<=now() AND patient_id=$1;`,
		userID,
	)

	query := `
		SELECT b.id, b.patient_id, COALESCE(p.full_name, '') AS patient_name,
		       b.caregiver_id, b.caregiver_name, b.caregiver_relation,
		       b.connection_code, COALESCE(b.status, 'PENDING') AS status, b.caregiver_role, b.permissions,
		       b.patient_public_key, b.caregiver_public_key, b.created_at, b.expires_at
		FROM family_bindings b
		LEFT JOIN profiles p ON p.id=b.patient_id
		WHERE b.patient_id=$1 OR b.caregiver_id=$1
		ORDER BY b.created_at DESC;
	`

	rows, err := r.pool.Query(ctx, query, userID)
	if err != nil {
		return nil, fmt.Errorf("falha ao listar vínculos: %w", err)
	}
	defer rows.Close()

	list := make([]domain.FamilyBinding, 0)
	for rows.Next() {
		var b domain.FamilyBinding
		err := rows.Scan(
			&b.ID, &b.PatientID, &b.PatientName,
			&b.CaregiverID, &b.CaregiverName, &b.CaregiverRelation,
			&b.ConnectionCode, &b.Status, &b.CaregiverRole, &b.Permissions,
			&b.PatientPublicKey, &b.CaregiverPublicKey, &b.CreatedAt, &b.ExpiresAt,
		)
		if err != nil {
			return nil, fmt.Errorf("falha ao escanear vínculo: %w", err)
		}
		b.Permissions = sanitizePermissions(b.Permissions)
		list = append(list, b)
	}

	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("erro durante iteração de vínculos: %w", err)
	}

	return list, nil
}

func sanitizePermissions(perms []string) []string {
	if len(perms) == 0 {
		return []string{}
	}
	return perms
}

// SyncBinding cria (PENDING) ou revoga (REVOKED) um convite familiar.
// Fase #7: substitui o endpoint POST /api/sync/family-binding que ainda vivia
// no Python legado, mantendo o mesmo contrato e as mesmas regras de autorização.
func (r *PostgresRepository) SyncBinding(
	ctx context.Context,
	req *domain.SyncFamilyBindingRequest,
	requesterID string,
) (*domain.FamilyBinding, error) {
	if r.pool == nil {
		return nil, ErrDatabaseUnavailable
	}

	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return nil, fmt.Errorf("falha ao iniciar transação de vínculo: %w", err)
	}
	defer tx.Rollback(ctx)

	if req.Status == "REVOKED" {
		var patientID string
		var caregiverID *string
		var previousStatus string
		err := tx.QueryRow(ctx,
			`SELECT patient_id, caregiver_id, COALESCE(status,'PENDING') FROM family_bindings WHERE id=$1 FOR UPDATE;`,
			req.ID,
		).Scan(&patientID, &caregiverID, &previousStatus)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return nil, ErrNotFound
			}
			return nil, fmt.Errorf("falha ao verificar vínculo para revogação: %w", err)
		}
		if requesterID != patientID && (caregiverID == nil || requesterID != *caregiverID) {
			return nil, ErrForbidden
		}
		if _, err := tx.Exec(ctx, `UPDATE family_bindings SET status='REVOKED' WHERE id=$1;`, req.ID); err != nil {
			return nil, fmt.Errorf("falha ao revogar vínculo: %w", err)
		}
		binding := &domain.FamilyBinding{
			ID: req.ID, PatientID: patientID, CaregiverID: caregiverID, Status: "REVOKED",
		}
		if err := notifyBindingRevocation(ctx, tx, binding, requesterID, previousStatus != "REVOKED"); err != nil {
			return nil, err
		}
		if err := tx.Commit(ctx); err != nil {
			return nil, err
		}
		return binding, nil
	}

	if requesterID != req.PatientID {
		return nil, ErrForbidden
	}

	bindingID := req.ID
	if bindingID == "" {
		bindingID = uuid.NewString()
	}
	now := time.Now().UTC()
	expiresAt := now.Add(7 * 24 * time.Hour)
	permissions := sanitizePermissions(req.Permissions)

	insertQuery := `
		INSERT INTO family_bindings (
			id, patient_id, caregiver_name, caregiver_relation, connection_code,
			status, created_at, expires_at, caregiver_role, permissions, patient_public_key
		) VALUES ($1,$2,$3,$4,$5,'PENDING',$6,$7,$8,$9,$10)
		ON CONFLICT DO NOTHING
		RETURNING id, patient_id, caregiver_id, caregiver_name, caregiver_relation,
		          connection_code, status, created_at, expires_at, caregiver_role,
		          permissions, patient_public_key, caregiver_public_key;
	`
	binding := &domain.FamilyBinding{}
	err = tx.QueryRow(ctx, insertQuery,
		bindingID, req.PatientID, req.CaregiverName, req.CaregiverRelation,
		req.ConnectionCode, now, expiresAt, req.CaregiverRole, permissions,
		req.PatientPublicKey,
	).Scan(
		&binding.ID, &binding.PatientID, &binding.CaregiverID, &binding.CaregiverName,
		&binding.CaregiverRelation, &binding.ConnectionCode, &binding.Status,
		&binding.CreatedAt, &binding.ExpiresAt, &binding.CaregiverRole,
		&binding.Permissions, &binding.PatientPublicKey, &binding.CaregiverPublicKey,
	)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, fmt.Errorf("falha ao criar vínculo familiar: %w", err)
	}

	if errors.Is(err, pgx.ErrNoRows) {
		// Conflito: idempotência apenas para o mesmo convite PENDING do dono.
		existing := &domain.FamilyBinding{}
		err = tx.QueryRow(ctx, `
			SELECT id, patient_id, caregiver_id, caregiver_name, caregiver_relation,
			       connection_code, status, created_at, expires_at, caregiver_role,
			       permissions, patient_public_key, caregiver_public_key
			FROM family_bindings WHERE id=$1;`, bindingID,
		).Scan(
			&existing.ID, &existing.PatientID, &existing.CaregiverID, &existing.CaregiverName,
			&existing.CaregiverRelation, &existing.ConnectionCode, &existing.Status,
			&existing.CreatedAt, &existing.ExpiresAt, &existing.CaregiverRole,
			&existing.Permissions, &existing.PatientPublicKey, &existing.CaregiverPublicKey,
		)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return nil, ErrConflict
			}
			return nil, fmt.Errorf("falha ao reconciliar convite existente: %w", err)
		}
		if existing.PatientID != requesterID ||
			existing.ConnectionCode != req.ConnectionCode ||
			existing.Status != "PENDING" {
			return nil, ErrConflict
		}
		if req.PatientPublicKey != nil && existing.PatientPublicKey == nil {
			if _, err := tx.Exec(ctx,
				`UPDATE family_bindings SET patient_public_key=$2 WHERE id=$1;`,
				bindingID, *req.PatientPublicKey,
			); err != nil {
				return nil, fmt.Errorf("falha ao atualizar chave pública do convite: %w", err)
			}
			existing.PatientPublicKey = req.PatientPublicKey
		}
		binding = existing
	}

	binding.Permissions = sanitizePermissions(binding.Permissions)
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return binding, nil
}

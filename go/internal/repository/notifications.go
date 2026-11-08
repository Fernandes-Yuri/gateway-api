package repository

import (
	"context"
	"fmt"
	"sort"

	"github.com/Fernandes-Yuri/gateway-api/go/internal/domain"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// A mensagem e os eventos usam o mesmo tx; nenhum conteúdo do chat ou envelope
// cifrado é copiado para a central/outbox. A ordem evita inversão de locks por conta.
func emitMessageNotifications(ctx context.Context, tx pgx.Tx, msg *domain.FamilyMessage) error {
	rows, err := tx.Query(ctx, `
		SELECT recipient FROM (
			SELECT $1::text AS recipient
			UNION
			SELECT caregiver_id::text FROM family_bindings
			WHERE patient_id=$1 AND status='ACTIVE' AND caregiver_id IS NOT NULL
		) audience WHERE recipient<>$2 ORDER BY recipient`, msg.PatientID, msg.SenderID)
	if err != nil {
		return fmt.Errorf("falha ao consultar destinatários da mensagem: %w", err)
	}
	var recipients []string
	for rows.Next() {
		var recipient string
		if err := rows.Scan(&recipient); err != nil {
			rows.Close()
			return err
		}
		recipients = append(recipients, recipient)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	for _, recipient := range recipients {
		if _, err := tx.Exec(ctx, `SELECT notification_emit(
			$1,$2,$3,'FAMILY_MESSAGE','FAMILY','chat',$4,$4,$5,$6,$7,'{}'::jsonb,true)`,
			recipient, messageEventKey(msg.ID), "chat:"+msg.PatientID, msg.PatientID, msg.SenderID,
			msg.SentAt, msg.ExpiresAt); err != nil {
			return fmt.Errorf("falha ao registrar aviso da mensagem: %w", err)
		}
	}
	return nil
}

func emitBindingNotification(ctx context.Context, tx pgx.Tx, binding *domain.FamilyBinding, actorID string) error {
	recipient := bindingNotificationRecipient(binding, actorID)
	if recipient == "" {
		return nil
	}
	kind := "FAMILY_BINDING_ACCEPTED"
	if binding.Status == "REVOKED" {
		kind = "FAMILY_BINDING_REVOKED"
	}
	id := canonicalNotificationUUID(binding.ID)
	if _, err := tx.Exec(ctx, `SELECT notification_emit(
		$1,$2,$3,$4,'FAMILY','binding',$5,$6,$7,now(),now()+interval '30 days',
		jsonb_build_object('bindingId',$5::text,'status',$8::text),true)`,
		recipient, "binding:"+id+":"+binding.Status, "binding:"+id, kind,
		id, binding.PatientID, actorID, binding.Status); err != nil {
		return fmt.Errorf("falha ao registrar aviso de vínculo: %w", err)
	}
	return nil
}

func canonicalNotificationUUID(id string) string {
	if parsed, err := uuid.Parse(id); err == nil {
		return parsed.String()
	}
	return id
}

func messageEventKey(id string) string { return "message:" + canonicalNotificationUUID(id) }

func bindingNotificationRecipient(binding *domain.FamilyBinding, actorID string) string {
	recipient := binding.PatientID
	if actorID == binding.PatientID {
		if binding.CaregiverID == nil {
			return ""
		}
		recipient = *binding.CaregiverID
	}
	if recipient == "" || recipient == actorID {
		return ""
	}
	return recipient
}

func notifyBindingRevocation(ctx context.Context, tx pgx.Tx, binding *domain.FamilyBinding, actorID string, changed bool) error {
	type action struct {
		recipient string
		run       func() error
	}
	var actions []action
	if binding.CaregiverID != nil {
		caregiverID := *binding.CaregiverID
		var stillActive bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM family_bindings
			WHERE patient_id=$1 AND caregiver_id=$2 AND status='ACTIVE')`, binding.PatientID, caregiverID).Scan(&stillActive); err != nil {
			return err
		}
		if !stillActive {
			actions = append(actions, action{caregiverID, func() error {
				_, err := tx.Exec(ctx, `SELECT notification_revoke_patient($1,$2)`, binding.PatientID, caregiverID)
				return err
			}})
		}
	}
	if recipient := bindingNotificationRecipient(binding, actorID); changed && recipient != "" {
		actions = append(actions, action{recipient, func() error {
			return emitBindingNotification(ctx, tx, binding, actorID)
		}})
	}
	sort.SliceStable(actions, func(i, j int) bool { return actions[i].recipient < actions[j].recipient })
	for _, next := range actions {
		if err := next.run(); err != nil {
			return fmt.Errorf("falha ao reconciliar avisos após revogação: %w", err)
		}
	}
	return nil
}

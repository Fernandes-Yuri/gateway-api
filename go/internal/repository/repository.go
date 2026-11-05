package repository

import (
	"context"
	"errors"
	"time"

	"github.com/Fernandes-Yuri/gateway-api/go/internal/domain"
)

var (
	ErrNotFound            = errors.New("recurso não encontrado")
	ErrForbidden           = errors.New("acesso não autorizado")
	ErrConflict            = errors.New("conflito de estado ou registro já existente")
	ErrDatabaseUnavailable = errors.New("banco de dados indisponível")
)

// FamilyRepository define as operações de persistência e consulta para mensagens e vínculos familiares
type FamilyRepository interface {
	// Mensagens
	SaveMessage(ctx context.Context, msg *domain.FamilyMessage) error
	GetMessages(ctx context.Context, patientID string, limit, offset int) ([]domain.FamilyMessage, error)
	DeleteMessage(ctx context.Context, messageID string, senderID string, patientID *string, sentAt *time.Time) error
	MarkMessagesAsRead(ctx context.Context, patientID string, readerID string) error

	// Vínculos & Acesso
	CheckFamilyAccess(ctx context.Context, uid string, patientID string) (bool, error)
	FindByConnectionCode(ctx context.Context, code string) (*domain.FamilyBinding, error)
	AcceptInvitation(ctx context.Context, code string, caregiverID string, caregiverName string, caregiverRelation string, caregiverPublicKey *string) (*domain.FamilyBinding, error)
	PublishPublicKey(ctx context.Context, bindingID *string, patientID *string, uid string, publicKey string) (*domain.PublishFamilyPublicKeyResponse, error)
	ListBindings(ctx context.Context, userID string) ([]domain.FamilyBinding, error)
}

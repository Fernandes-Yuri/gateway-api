package domain

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

type FixtureFile struct {
	Description string `json:"description"`
	Version     string `json:"version"`
	Fixtures    struct {
		SendE2EEMessage struct {
			Request struct {
				Body SendFamilyMessageRequest `json:"body"`
			} `json:"request"`
		} `json:"send_e2ee_message"`
		PublishECDHPublicKey struct {
			Request struct {
				Body PublishFamilyPublicKeyRequest `json:"body"`
			} `json:"request"`
		} `json:"publish_ecdh_public_key"`
		AcceptInvitation struct {
			Request struct {
				Body AcceptInvitationRequest `json:"body"`
			} `json:"request"`
		} `json:"accept_invitation"`
	} `json:"fixtures"`
}

func TestFixturesParityWithGo(t *testing.T) {
	fixturePath := filepath.Join("..", "..", "..", "tests", "contracts", "family", "fixtures_family_chat.json")
	data, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatalf("Erro ao ler fixtures JSON: %v", err)
	}

	var ff FixtureFile
	if err := json.Unmarshal(data, &ff); err != nil {
		t.Fatalf("Erro ao deserializar fixtures JSON: %v", err)
	}

	t.Run("Send E2EE Message Envelope Parity", func(t *testing.T) {
		body := ff.Fixtures.SendE2EEMessage.Request.Body
		if body.BodyEncrypted == nil {
			t.Fatal("bodyEncrypted não deveria ser nulo")
		}
		parsed, err := ParseE2EEEnvelope(*body.BodyEncrypted, true)
		if err != nil {
			t.Fatalf("Esperava validação bem-sucedida do envelope, erro: %v", err)
		}
		if parsed == "" {
			t.Error("Envelope compactado não pode ser vazio")
		}
	})

	t.Run("Publish ECDH Public Key Parity", func(t *testing.T) {
		key := ff.Fixtures.PublishECDHPublicKey.Request.Body.PublicKey
		validated, err := ValidateE2EEPublicKey(key)
		if err != nil {
			t.Fatalf("Esperava validação bem-sucedida da chave pública, erro: %v", err)
		}
		if validated != key {
			t.Errorf("Esperava %s, obteve %s", key, validated)
		}
	})

	t.Run("Accept Invitation Public Key Parity", func(t *testing.T) {
		req := ff.Fixtures.AcceptInvitation.Request.Body
		if req.CaregiverPublicKey == nil {
			t.Fatal("caregiverPublicKey não deveria ser nulo")
		}
		validated, err := ValidateE2EEPublicKey(*req.CaregiverPublicKey)
		if err != nil {
			t.Fatalf("Esperava validação bem-sucedida da chave do cuidador, erro: %v", err)
		}
		if validated != *req.CaregiverPublicKey {
			t.Errorf("Esperava %s, obteve %s", *req.CaregiverPublicKey, validated)
		}
	})
}

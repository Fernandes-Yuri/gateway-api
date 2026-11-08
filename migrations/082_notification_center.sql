SET lock_timeout = '5s';
SET statement_timeout = '120s';

-- Um grupo estável por destinatário; fontes individuais permitem reconciliar
-- mensagens realmente lidas sem confundir recibos de chat com leitura da caixa.
CREATE SEQUENCE IF NOT EXISTS notification_revision_seq;
CREATE TABLE IF NOT EXISTS notification_items (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    recipient_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    group_key text NOT NULL,
    revision bigint NOT NULL DEFAULT 0,
    snapshot jsonb NOT NULL DEFAULT '{}',
    UNIQUE(recipient_id, group_key)
);
CREATE TABLE IF NOT EXISTS notification_sources (
    item_id uuid NOT NULL REFERENCES notification_items(id) ON DELETE CASCADE,
    event_key text NOT NULL,
    event_revision bigint NOT NULL,
    kind text NOT NULL,
    category text NOT NULL CHECK(category IN ('FAMILY','AGENDA','COMMUNITY','CARE')),
    resource_type text NOT NULL,
    resource_id text NOT NULL,
    patient_id text,
    actor_id text,
    occurred_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    read_at timestamptz,
    archived_at timestamptz,
    active boolean NOT NULL DEFAULT true,
    payload jsonb NOT NULL DEFAULT '{}'
        CHECK(jsonb_typeof(payload)='object' AND payload - ARRAY['reaction','status','bindingId']::text[] = '{}'::jsonb),
    PRIMARY KEY(item_id,event_key)
);
CREATE INDEX IF NOT EXISTS notification_sources_key ON notification_sources(event_key);
CREATE INDEX IF NOT EXISTS notification_sources_target ON notification_sources(resource_type,resource_id);
CREATE TABLE IF NOT EXISTS notification_changes (
    revision bigint PRIMARY KEY,
    recipient_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    item_id uuid NOT NULL REFERENCES notification_items(id) ON DELETE CASCADE,
    snapshot jsonb NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notification_changes_owner_revision ON notification_changes(recipient_id,revision);
CREATE INDEX IF NOT EXISTS notification_changes_item_revision ON notification_changes(recipient_id,item_id,revision DESC);
CREATE TABLE IF NOT EXISTS notification_outbox (
    id bigserial PRIMARY KEY,
    item_id uuid NOT NULL REFERENCES notification_items(id) ON DELETE CASCADE,
    recipient_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    event_key text NOT NULL,
    revision bigint NOT NULL,
    kind text NOT NULL,
    state text NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','SENDING','DELIVERED','SKIPPED','FAILED')),
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_id uuid,
    lease_until timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(item_id,revision)
);
CREATE INDEX IF NOT EXISTS notification_outbox_ready ON notification_outbox(available_at,id)
    WHERE state IN ('PENDING','SENDING');
CREATE TABLE IF NOT EXISTS notification_push_deliveries (
    outbox_id bigint NOT NULL REFERENCES notification_outbox(id) ON DELETE CASCADE,
    device_hash text NOT NULL,
    delivered_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(outbox_id,device_hash)
);

CREATE OR REPLACE FUNCTION notification_lock_pair(a text,b text) RETURNS void LANGUAGE sql AS $$
    SELECT pg_advisory_xact_lock(hashtextextended('social:' || least(a,b) || ':' || greatest(a,b),8290));
$$;

-- PL/pgSQL resolve cada tabela apenas no ramo usado. Não depende de nomes,
-- texto de mensagens, envelopes E2EE ou de informações clínicas no payload.
CREATE OR REPLACE FUNCTION notification_source_allowed(
    k text,recipient text,target_type text,target_id text,patient text,actor text,event_key text
) RETURNS boolean LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF NOT EXISTS(SELECT 1 FROM profiles p WHERE p.id=recipient
                  AND to_jsonb(p)->>'paused_at' IS NULL) THEN RETURN false; END IF;
    IF k='FAMILY_MESSAGE' THEN
        RETURN EXISTS(SELECT 1 FROM family_messages m WHERE m.id::text=substr(event_key,9)
            AND m.patient_id=patient AND m.sender_id=actor AND m.deleted_at IS NULL AND m.expires_at>now())
            AND (recipient=patient OR EXISTS(SELECT 1 FROM family_bindings b
                WHERE b.patient_id=patient AND b.caregiver_id=recipient AND b.status='ACTIVE'));
    ELSIF k LIKE 'CONSULTATION_%' THEN
        RETURN EXISTS(SELECT 1 FROM consultations c WHERE c.id::text=target_id AND c.user_id=patient)
            AND (recipient=patient OR EXISTS(SELECT 1 FROM family_bindings b
                WHERE b.patient_id=patient AND b.caregiver_id=recipient AND b.status='ACTIVE'));
    ELSIF k LIKE 'FAMILY_BINDING_%' THEN
        RETURN EXISTS(SELECT 1 FROM family_bindings b WHERE b.id::text=target_id
            AND recipient IN (b.patient_id,b.caregiver_id));
    ELSIF k='POST_REACTION' THEN
        RETURN recipient<>actor AND EXISTS(SELECT 1 FROM social_posts p
            JOIN post_reactions r ON r.post_id=p.id AND r.user_id=actor
            WHERE p.id::text=target_id AND p.user_id=recipient AND p.is_visible
              AND NOT EXISTS(SELECT 1 FROM social_user_blocks b
                WHERE (b.user_id=recipient AND b.blocked_user_id=actor)
                   OR (b.user_id=actor AND b.blocked_user_id=recipient))
              AND (p.visibility='PUBLIC' OR EXISTS(SELECT 1 FROM family_bindings b
                WHERE b.status='ACTIVE' AND ((b.patient_id=recipient AND b.caregiver_id=actor)
                    OR (b.patient_id=actor AND b.caregiver_id=recipient)))));
    ELSIF k IN ('MEDICATION_LOW_STOCK','MEDICATION_REMINDER','CARE_ALERT') THEN
        RETURN (recipient=patient OR EXISTS(SELECT 1 FROM family_bindings b
            WHERE b.patient_id=patient AND b.caregiver_id=recipient AND b.status='ACTIVE'))
            AND (target_type<>'medication' OR EXISTS(SELECT 1 FROM medications m
                WHERE m.id::text=target_id AND m.user_id=patient AND to_jsonb(m)->>'deleted_at' IS NULL));
    END IF;
    RETURN false;
END $$;

CREATE OR REPLACE FUNCTION notification_refresh(item uuid,revision_value bigint DEFAULT NULL)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE owner text; rev bigint; latest notification_sources%ROWTYPE; total integer; unread integer;
        unarchived integer; read_stamp timestamptz; data jsonb;
BEGIN
    SELECT recipient_id INTO owner FROM notification_items WHERE id=item;
    IF owner IS NULL THEN RETURN NULL; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(owner,8282));
    rev := COALESCE(revision_value,nextval('notification_revision_seq'));
    SELECT * INTO latest FROM notification_sources WHERE item_id=item AND active AND expires_at>now()
        ORDER BY event_revision DESC,event_key DESC LIMIT 1;
    SELECT count(*),count(*) FILTER(WHERE read_at IS NULL AND archived_at IS NULL),
           count(*) FILTER(WHERE archived_at IS NULL),max(read_at)
        INTO total,unread,unarchived,read_stamp FROM notification_sources
        WHERE item_id=item AND active AND expires_at>now();
    IF total=0 THEN
        data := jsonb_build_object('id',item,'revision',rev,'deleted',true);
    ELSE
        data := jsonb_build_object('id',item,'revision',rev,'deleted',false,
            'kind',latest.kind,'category',latest.category,'actorId',latest.actor_id,
            'patientId',latest.patient_id,'resourceType',latest.resource_type,'resourceId',latest.resource_id,
            'occurredAt',latest.occurred_at,'expiresAt',latest.expires_at,'count',total,
            'unreadCount',unread,'isRead',unread=0,'readAt',CASE WHEN unread=0 THEN read_stamp END,
            'archived',unarchived=0,'data',latest.payload);
    END IF;
    UPDATE notification_items SET revision=rev,snapshot=data WHERE id=item;
    INSERT INTO notification_changes(revision,recipient_id,item_id,snapshot) VALUES(rev,owner,item,data);
    RETURN rev;
END $$;

CREATE OR REPLACE FUNCTION notification_emit(
    recipient text,event_key_value text,group_key_value text,kind_value text,category_value text,
    resource_type_value text,resource_id_value text,patient_value text,actor_value text,
    occurred_value timestamptz,expires_value timestamptz DEFAULT NULL,
    payload_value jsonb DEFAULT '{}',realert boolean DEFAULT true
) RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE item uuid; previous notification_sources%ROWTYPE; rev bigint; expiry timestamptz; fresh boolean;
BEGIN
    IF recipient IS NULL OR recipient=actor_value THEN RETURN NULL; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(recipient,8282));
    IF NOT notification_source_allowed(kind_value,recipient,resource_type_value,resource_id_value,
                                     patient_value,actor_value,event_key_value) THEN RETURN NULL; END IF;
    expiry := COALESCE(expires_value,COALESCE(occurred_value,now())+interval '30 days');
    IF expiry<=now() THEN RETURN NULL; END IF;
    INSERT INTO notification_items(recipient_id,group_key) VALUES(recipient,group_key_value)
        ON CONFLICT(recipient_id,group_key) DO NOTHING;
    SELECT id INTO item FROM notification_items WHERE recipient_id=recipient AND group_key=group_key_value;
    SELECT * INTO previous FROM notification_sources WHERE item_id=item AND event_key=event_key_value;
    IF FOUND AND previous.active AND previous.payload=COALESCE(payload_value,'{}')
        AND previous.kind=kind_value AND previous.resource_id=resource_id_value THEN RETURN item; END IF;
    fresh := previous.item_id IS NULL OR NOT previous.active;
    rev := nextval('notification_revision_seq');
    INSERT INTO notification_sources(item_id,event_key,event_revision,kind,category,resource_type,resource_id,
        patient_id,actor_id,occurred_at,expires_at,payload)
        VALUES(item,event_key_value,rev,kind_value,category_value,resource_type_value,resource_id_value,
            patient_value,actor_value,COALESCE(occurred_value,now()),expiry,COALESCE(payload_value,'{}'))
        ON CONFLICT(item_id,event_key) DO UPDATE SET active=true,kind=excluded.kind,payload=excluded.payload,
            expires_at=excluded.expires_at,
            occurred_at=CASE WHEN realert OR fresh THEN excluded.occurred_at ELSE notification_sources.occurred_at END,
            event_revision=CASE WHEN realert OR fresh THEN excluded.event_revision ELSE notification_sources.event_revision END,
            read_at=CASE WHEN realert OR fresh THEN NULL ELSE notification_sources.read_at END,
            archived_at=CASE WHEN realert OR fresh THEN NULL ELSE notification_sources.archived_at END;
    PERFORM notification_refresh(item,rev);
    IF fresh OR realert THEN
        INSERT INTO notification_outbox(item_id,recipient_id,event_key,revision,kind)
            VALUES(item,recipient,event_key_value,rev,kind_value) ON CONFLICT DO NOTHING;
    END IF;
    RETURN item;
END $$;

CREATE OR REPLACE FUNCTION notification_remove(recipient text,key_value text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE item uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(recipient,8282));
    FOR item IN UPDATE notification_sources s SET active=false FROM notification_items i
        WHERE s.item_id=i.id AND i.recipient_id=recipient AND s.event_key=key_value AND s.active
        RETURNING s.item_id LOOP PERFORM notification_refresh(item); END LOOP;
END $$;
CREATE OR REPLACE FUNCTION notification_remove_event(key_value text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE owner text;
BEGIN
    FOR owner IN SELECT DISTINCT i.recipient_id FROM notification_items i JOIN notification_sources s ON s.item_id=i.id
        WHERE s.event_key=key_value AND s.active ORDER BY i.recipient_id
        LOOP PERFORM notification_remove(owner,key_value); END LOOP;
END $$;
CREATE OR REPLACE FUNCTION notification_remove_resource(target_type text,target_id text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE entry record;
BEGIN
    FOR entry IN SELECT i.recipient_id,s.event_key FROM notification_items i JOIN notification_sources s ON s.item_id=i.id
        WHERE s.resource_type=target_type AND s.resource_id=target_id AND s.active ORDER BY i.recipient_id,s.event_key
        LOOP PERFORM notification_remove(entry.recipient_id,entry.event_key); END LOOP;
END $$;
CREATE OR REPLACE FUNCTION notification_read_sources(recipient text,keys_value text[]) RETURNS void LANGUAGE plpgsql AS $$
DECLARE item uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(recipient,8282));
    FOR item IN WITH changed AS (UPDATE notification_sources s SET read_at=now() FROM notification_items i
        WHERE s.item_id=i.id AND i.recipient_id=recipient AND s.event_key=ANY(keys_value)
            AND s.active AND s.read_at IS NULL RETURNING s.item_id)
        SELECT DISTINCT item_id FROM changed LOOP PERFORM notification_refresh(item); END LOOP;
END $$;
CREATE OR REPLACE FUNCTION notification_prune(recipient text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE item uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(recipient,8282));
    FOR item IN WITH changed AS (UPDATE notification_sources s SET active=false FROM notification_items i
        WHERE s.item_id=i.id AND i.recipient_id=recipient AND s.active AND (s.expires_at<=now()
            OR NOT notification_source_allowed(s.kind,recipient,s.resource_type,s.resource_id,s.patient_id,s.actor_id,s.event_key))
        RETURNING s.item_id) SELECT DISTINCT item_id FROM changed
        LOOP PERFORM notification_refresh(item); END LOOP;
END $$;
CREATE OR REPLACE FUNCTION notification_revoke_patient(patient text,recipient text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    -- Outro vínculo ACTIVE do mesmo paciente mantém acesso autorizado.
    PERFORM notification_prune(recipient);
END $$;
CREATE OR REPLACE FUNCTION notification_block_pair(a text,b text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE entry record;
BEGIN
    PERFORM notification_lock_pair(a,b);
    FOR entry IN SELECT i.recipient_id,s.event_key FROM notification_items i JOIN notification_sources s ON s.item_id=i.id
        WHERE s.active AND s.kind='POST_REACTION' AND ((i.recipient_id=a AND s.actor_id=b) OR (i.recipient_id=b AND s.actor_id=a))
        ORDER BY i.recipient_id,s.event_key
        LOOP PERFORM notification_remove(entry.recipient_id,entry.event_key); END LOOP;
END $$;
CREATE OR REPLACE FUNCTION notification_mark(recipient text,item_value uuid,through_value bigint,archive_value boolean DEFAULT false)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE item uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(recipient,8282));
    FOR item IN WITH changed AS (UPDATE notification_sources s SET
            read_at=COALESCE(s.read_at,now()),archived_at=CASE WHEN archive_value THEN COALESCE(s.archived_at,now()) ELSE s.archived_at END
        FROM notification_items i WHERE s.item_id=i.id AND i.recipient_id=recipient
            AND (item_value IS NULL OR i.id=item_value) AND s.event_revision<=through_value AND s.active
            AND (s.read_at IS NULL OR (archive_value AND s.archived_at IS NULL)) RETURNING s.item_id)
        SELECT DISTINCT item_id FROM changed LOOP PERFORM notification_refresh(item); END LOOP;
END $$;

DO $$
BEGIN
    IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='bragasaude_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON notification_items,notification_sources,notification_changes,notification_outbox,notification_push_deliveries TO bragasaude_app;
        GRANT USAGE, SELECT ON SEQUENCE notification_revision_seq,notification_outbox_id_seq TO bragasaude_app;
        GRANT EXECUTE ON FUNCTION
            notification_lock_pair(text,text),
            notification_source_allowed(text,text,text,text,text,text,text),
            notification_refresh(uuid,bigint),
            notification_emit(text,text,text,text,text,text,text,text,text,timestamptz,timestamptz,jsonb,boolean),
            notification_remove(text,text),
            notification_remove_event(text),
            notification_remove_resource(text,text),
            notification_read_sources(text,text[]),
            notification_prune(text),
            notification_revoke_patient(text,text),
            notification_block_pair(text,text),
            notification_mark(text,uuid,bigint,boolean)
        TO bragasaude_app;
    END IF;
END $$;

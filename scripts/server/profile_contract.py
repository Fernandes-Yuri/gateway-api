"""Shared profile validation, independent of network and database connections."""
from datetime import date, datetime
import math


def validate_profile(p):
    role, mode = p.userRole, p.caregiverMode
    if role not in (None, 'PATIENT', 'CAREGIVER'):
        raise ValueError('Papel de usuário inválido.')
    if mode not in (None, 'VIEWER_ONLY', 'HYBRID') or (mode is not None and role != 'CAREGIVER'):
        raise ValueError('Modo de uso incompatível com o perfil.')
    if p.basicProfileComplete and (not p.fullName.strip() or role is None):
        raise ValueError('Informe o nome e o modo de uso para concluir o cadastro.')
    if p.basicProfileComplete and role == 'CAREGIVER' and mode is None:
        raise ValueError('Escolha o modo de acompanhamento.')
    if p.selfCareComplete:
        if not p.basicProfileComplete or (role == 'CAREGIVER' and mode != 'HYBRID'):
            raise ValueError('Autocuidado incompatível com o cadastro.')
        if (p.weight is None or p.height is None or not math.isfinite(p.weight)
                or not math.isfinite(p.height) or not 20 <= p.weight <= 350 or not 50 <= p.height <= 250):
            raise ValueError('Informe peso entre 20 e 350 kg e altura entre 50 e 250 cm.')
    if p.selfCareSetupPending and (role != 'CAREGIVER' or mode != 'VIEWER_ONLY'):
        raise ValueError('A ativação pendente é exclusiva do modo acompanhante.')
    if p.birthDate:
        date.fromisoformat(p.birthDate)


def timestamp(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('O horário deve conter fuso horário.')
    return parsed


# Only these fixed identifiers may be interpolated into SQL.
PROFILE_FIELDS = {
    'id': 'id', 'full_name': 'fullName', 'birth_date': 'birthDate', 'gender': 'gender',
    'height': 'height', 'weight': 'weight', 'is_smoker': 'isSmoker',
    'has_diabetes': 'hasDiabetes', 'has_hypertension': 'hasHypertension',
    'has_thyroid_issue': 'hasThyroidIssue', 'has_renal_issue': 'hasRenalIssue',
    'has_bone_issue': 'hasBoneIssue', 'has_muscular_issue': 'hasMuscularIssue',
    'hydration_target_ml': 'hydrationTargetMl', 'daily_calorie_target': 'dailyCalorieTarget',
    'step_goal': 'stepGoal', 'weight_goal': 'weightGoal', 'sleep_start_time': 'sleepStartTime',
    'sleep_end_time': 'sleepEndTime', 'emergency_contact_name': 'emergencyContactName',
    'emergency_contact_relation': 'emergencyContactRelation', 'emergency_contact_phone': 'emergencyContactPhone',
    'consent_accepted_at': 'consentAcceptedAt', 'notifications_enabled': 'notificationsEnabled',
    'location_enabled': 'locationEnabled', 'current_xp': 'currentXp', 'total_xp': 'totalXp',
    'current_level': 'currentLevel', 'current_streak': 'currentStreak', 'show_in_feed': 'showInFeed',
    'user_role': 'userRole', 'caregiver_mode': 'caregiverMode', 'avatar_identifier': 'avatarIdentifier',
    'basic_profile_complete': 'basicProfileComplete', 'self_care_complete': 'selfCareComplete',
    'self_care_setup_pending': 'selfCareSetupPending', 'diabetes_type': 'diabetesType',
    'food_allergies': 'foodAllergies', 'custom_food_restrictions': 'customFoodRestrictions',
    'activity_level': 'activityLevel', 'phone': 'phone',
}


def profile_upsert(p):
    validate_profile(p)
    values = [getattr(p, field) for field in PROFILE_FIELDS.values()]
    keys = list(PROFILE_FIELDS)
    values[keys.index('birth_date')] = date.fromisoformat(p.birthDate) if p.birthDate else None
    values[keys.index('consent_accepted_at')] = timestamp(p.consentAcceptedAt)
    assignments = ', '.join(f'{key} = EXCLUDED.{key}' for key in keys if key != 'id')
    placeholders = ', '.join(f'${i}' for i in range(1, len(keys) + 1))
    sql = f"INSERT INTO profiles ({', '.join(keys)}) VALUES ({placeholders}) ON CONFLICT (id) DO UPDATE SET {assignments}, updated_at = NOW()"
    return sql, values

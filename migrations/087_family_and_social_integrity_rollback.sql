ALTER TABLE family_bindings DROP CONSTRAINT IF EXISTS family_bindings_no_self_caregiver;
ALTER TABLE family_bindings DROP CONSTRAINT IF EXISTS family_bindings_status_enum;
ALTER TABLE social_posts DROP CONSTRAINT IF EXISTS social_posts_visibility_enum;
ALTER TABLE profiles DROP CONSTRAINT IF EXISTS profiles_user_role_enum;
ALTER TABLE profiles DROP CONSTRAINT IF EXISTS profiles_sleep_start_format;
ALTER TABLE profiles DROP CONSTRAINT IF EXISTS profiles_sleep_end_format;
ALTER TABLE profiles DROP CONSTRAINT IF EXISTS profiles_hydration_target_range;
ALTER TABLE profiles DROP CONSTRAINT IF EXISTS profiles_step_goal_range;

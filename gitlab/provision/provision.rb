# frozen_string_literal: true

# Configuration de base de GitLab pour le lab, exécutée par provision.sh avec « gitlab-rails runner »
# à chaque démarrage. Idempotente : ne crée que ce qui manque.
#
#   GITLAB_USERS      « user:motdepasse,user2:motdepasse2 » : comptes créés s'ils n'existent pas.
#                     Le mot de passe du .env s'applique tant que le compte n'a jamais servi à se
#                     connecter à l'interface ; ensuite, celui choisi par l'utilisateur est conservé.
#                     8 caractères minimum : GitLab refuse les mots de passe plus courts, même à la
#                     connexion (EncryptedUserPassword).
#   GITLAB_GROUP      groupe dont ces comptes sont Owner
#   GITLAB_API_TOKEN  jeton d'API de root (20 caractères), utilisé par les services gitlab-sync et
#                     gitlab-runner (enregistrement du runner : le scope api suffit)
#
# Le jeton est traité en dernier : quand il est valide, gitlab-sync et gitlab-runner savent que le
# reste est en place.
#
# S'appuie sur des classes internes de GitLab (Users::CreateService, Groups::CreateService,
# PersonalAccessToken#set_token), vérifiées sur GitLab CE 19.6 : à revérifier si l'image change de
# version majeure ou mineure.

$stdout.sync = true

TOKEN_NAME = 'sonic-lab-sync'
TOKEN_SCOPES = %w[api read_repository write_repository].freeze
TOKEN_LIFETIME = 365.days # maximum autorisé par GitLab
TOKEN_RENEW_BEFORE = 30.days
EMAIL_DOMAIN = 'sonic-lab.local'

@errors = 0

def log(message)
  puts "[sonic-lab] #{message}"
end

# Exécute une étape ; une erreur est journalisée sans empêcher les étapes suivantes.
def step(name)
  yield
rescue StandardError => e
  @errors += 1
  log("ERREUR (#{name}) : #{e.class} : #{e.message}")
end

root = User.find_by_username('root') || User.admins.order(:id).first
raise 'aucun administrateur GitLab trouvé' unless root

organization = Organizations::Organization.default_organization

users_spec = ENV.fetch('GITLAB_USERS', '').split(/[\s,]+/).reject(&:empty?).map { |entry| entry.split(':', 2) }
group_path = ENV.fetch('GITLAB_GROUP', '').strip
token_value = ENV.fetch('GITLAB_API_TOKEN', '').strip

# --- 1. Réglages de l'instance ------------------------------------------------------------------
# Comptes définis par le .env : pas d'inscription libre. Pas de remontée de statistiques ni de
# vérification de version (comme GF_ANALYTICS_* pour Grafana). Pas d'Auto DevOps : avec le runner du
# lab, il lancerait un pipeline (voué à l'échec) sur chaque projet sans .gitlab-ci.yml.
step('réglages') do
  settings = ApplicationSetting.current_without_cache || ApplicationSetting.create_from_defaults
  wanted = { signup_enabled: false, usage_ping_enabled: false, version_check_enabled: false, auto_devops_enabled: false }
  changes = wanted.reject { |attribute, value| settings.public_send(attribute) == value }
  if changes.any?
    settings.update!(changes)
    log("réglages appliqués : #{changes.keys.join(', ')}")
  end
end

# --- 2. Utilisateurs -----------------------------------------------------------------------------
MIN_PASSWORD_LENGTH = ApplicationSetting::DEFAULT_MINIMUM_PASSWORD_LENGTH

# Mot de passe du .env, même s'il contient le nom d'utilisateur (refusé par la validation de GitLab,
# Security::WeakPasswords) : d'où validate: false. La longueur minimale, elle, ne se contourne pas.
def apply_password(user, password)
  user.password = password
  user.password_confirmation = password
  user.password_expires_at = nil
  user.save!(validate: false)
end

users = []
users_spec.each do |username, password|
  step("utilisateur #{username}") do
    raise 'format attendu : utilisateur:motdepasse' if password.to_s.empty?
    if password.length < MIN_PASSWORD_LENGTH
      raise "mot de passe trop court (#{password.length} caractères) : GitLab en exige au moins " \
        "#{MIN_PASSWORD_LENGTH}, même à la connexion. Compte ignoré."
    end

    user = User.find_by_username(username)
    if user.nil?
      response = Users::CreateService.new(root, {
        username: username,
        name: username,
        email: "#{username}@#{EMAIL_DOMAIN}",
        password: User.random_password, # temporaire, remplacé ci-dessous
        skip_confirmation: true,
        organization_id: organization.id
      }).execute
      raise response.message if response.error?

      user = response.payload[:user]
      apply_password(user, password)
      log("utilisateur #{username} créé")
    elsif user.sign_in_count.zero? && !user.valid_password?(password)
      apply_password(user, password)
      log("utilisateur #{username} : mot de passe du .env appliqué (compte jamais utilisé)")
    end
    users << user
  end
end

# --- 3. Groupe -----------------------------------------------------------------------------------
unless group_path.empty?
  step("groupe #{group_path}") do
    group = Group.find_by_full_path(group_path)
    unless group
      response = Groups::CreateService.new(root, {
        name: group_path,
        path: group_path,
        visibility_level: Gitlab::VisibilityLevel::PRIVATE,
        organization_id: organization.id
      }).execute
      group = response.payload[:group]
      raise "#{response.message} : #{group&.errors&.full_messages&.to_sentence}" if response.error?

      log("groupe #{group_path} créé")
    end

    users.each do |user|
      next if group.owned_by?(user)

      member = group.add_owner(user, root)
      raise "#{user.username} : #{member.errors.full_messages.to_sentence}" if member.errors.any?

      log("#{user.username} est Owner de #{group_path}")
    end
  end
end

# --- 4. Jeton d'API de root (en dernier) ---------------------------------------------------------
step('jeton API') do
  raise 'GITLAB_API_TOKEN est vide' if token_value.empty?

  log("GITLAB_API_TOKEN fait #{token_value.length} caractères, GitLab en attend 20") if token_value.length != 20

  token = PersonalAccessToken.find_by_token(token_value)
  raise "GITLAB_API_TOKEN est déjà le jeton de #{token.user.username}" if token && token.user_id != root.id

  # Un jeton révoqué dans GitLab n'est jamais réactivé : la révocation (fuite, rotation) l'emporte sur
  # le .env, qu'il faut alors changer.
  if token&.revoked?
    raise 'GITLAB_API_TOKEN a été révoqué dans GitLab : mettez une nouvelle valeur dans .env, puis make up'
  end

  if token.nil?
    token = root.personal_access_tokens.create(name: TOKEN_NAME, scopes: TOKEN_SCOPES,
      expires_at: TOKEN_LIFETIME.from_now.to_date)
    token.set_token(token_value)
    token.save!
    log('jeton API de root créé')
  elsif token.expires_at.nil? || token.expires_at < TOKEN_RENEW_BEFORE.from_now.to_date ||
      token.scopes.map(&:to_s).sort != TOKEN_SCOPES.sort
    token.update!(scopes: TOKEN_SCOPES, expires_at: TOKEN_LIFETIME.from_now.to_date)
    log('jeton API de root renouvelé')
  end

  # un seul jeton actif pour gitlab-sync : celui du .env
  root.personal_access_tokens.where(name: TOKEN_NAME, revoked: false).where.not(id: token.id).find_each(&:revoke!)
end

exit(@errors.zero? ? 0 : 1)

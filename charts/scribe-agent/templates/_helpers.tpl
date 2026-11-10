{{/* Shared naming, label and image helpers. */}}

{{- define "scribe-agent.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "scribe-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "scribe-agent.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "scribe-agent.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: clinical-scribe
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
Image reference. image.registry wins; otherwise global.imageRegistry is used,
which lets one value point every first-party image at a private registry.
*/}}
{{- define "scribe-agent.image" -}}
{{- $registry := .Values.image.registry | default ((.Values.global).imageRegistry) -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry .Values.image.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "scribe-agent.imagePullSecrets" -}}
{{- $secrets := concat (.Values.imagePullSecrets | default list) ((.Values.global).imagePullSecrets | default list) | uniq -}}
{{- if $secrets }}
imagePullSecrets:
{{- range $secrets }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "scribe-agent.scheduling" -}}
{{- with .Values.nodeSelector }}
nodeSelector:
{{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
{{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
{{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/*
Resolved database settings. Each key falls back from <chart>.database to
global.database, so the umbrella chart can take one database input and a
single component can still override any field.
*/}}
{{- define "scribe-agent.database" -}}
{{- $global := (.Values.global).database | default dict -}}
{{- $local := .Values.database | default dict -}}
{{- $db := dict -}}
{{- range $key := list "host" "port" "name" "username" "password" "existingSecret" "existingSecretUsernameKey" "existingSecretPasswordKey" "sslMode" "jdbcParams" -}}
{{- $_ := set $db $key (get $local $key | default (get $global $key)) -}}
{{- end -}}
{{- if not $db.host -}}
{{- fail "database host is required: set global.database.host or database.host" -}}
{{- end -}}
{{- if not $db.name -}}
{{- fail "database name is required: set global.database.name or database.name" -}}
{{- end -}}
{{- if and (not $db.existingSecret) (not $db.password) -}}
{{- fail "database credentials are required: set database.password or database.existingSecret" -}}
{{- end -}}
{{- if and (not $db.username) (not $db.existingSecretUsernameKey) -}}
{{- fail "database username is required: set database.username or database.existingSecretUsernameKey" -}}
{{- end -}}
{{- $_ := set $db "port" (int ($db.port | default 5432)) -}}
{{- $_ := set $db "sslMode" ($db.sslMode | default "require") -}}
{{- $_ := set $db "existingSecretPasswordKey" ($db.existingSecretPasswordKey | default "password") -}}
{{- toYaml $db -}}
{{- end -}}

{{- define "scribe-agent.databaseSecretName" -}}
{{- $db := include "scribe-agent.database" . | fromYaml -}}
{{- $db.existingSecret | default (printf "%s-db" (include "scribe-agent.fullname" .)) -}}
{{- end -}}

{{/* Env vars for the database username and password, read from a Secret. */}}
{{- define "scribe-agent.databaseCredentialEnv" -}}
{{- $db := include "scribe-agent.database" .ctx | fromYaml -}}
{{- $secret := include (printf "%s.databaseSecretName" .chart) .ctx -}}
- name: {{ .userVar }}
{{- if and $db.existingSecret $db.existingSecretUsernameKey }}
  valueFrom:
    secretKeyRef:
      name: {{ $secret }}
      key: {{ $db.existingSecretUsernameKey }}
{{- else }}
  value: {{ $db.username | quote }}
{{- end }}
- name: {{ .passwordVar }}
  valueFrom:
    secretKeyRef:
      name: {{ $secret }}
      key: {{ if $db.existingSecret }}{{ $db.existingSecretPasswordKey }}{{ else }}password{{ end }}
{{- end -}}

{{/*
Resolved LLM settings. Each key falls back from llm.* to global.llm.*.
*/}}
{{- define "scribe-agent.llm" -}}
{{- $global := (.Values.global).llm | default dict -}}
{{- $local := .Values.llm | default dict -}}
{{- $llm := dict -}}
{{- range $key := list "provider" "model" "apiKey" "existingSecret" "existingSecretKey" "baseUrl" -}}
{{- $_ := set $llm $key (get $local $key | default (get $global $key)) -}}
{{- end -}}
{{- $_ := set $llm "provider" ($llm.provider | default "anthropic") -}}
{{- if not (has $llm.provider (list "anthropic" "openai-compatible")) -}}
{{- fail (printf "llm.provider must be anthropic or openai-compatible, got %q" $llm.provider) -}}
{{- end -}}
{{- $defaultModel := ternary "claude-opus-5-5" "Qwen/Qwen2.5-7B-Instruct" (eq $llm.provider "anthropic") -}}
{{- $_ := set $llm "model" ($llm.model | default $defaultModel) -}}
{{- $_ := set $llm "existingSecretKey" ($llm.existingSecretKey | default "api-key") -}}
{{- if and (eq $llm.provider "anthropic") (not $llm.apiKey) (not $llm.existingSecret) -}}
{{- fail "a Claude API key is required for llm.provider=anthropic: set global.llm.apiKey or global.llm.existingSecret" -}}
{{- end -}}
{{- toYaml $llm -}}
{{- end -}}

{{/* Shared naming, label and image helpers. */}}

{{- define "fhir-server.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "fhir-server.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "fhir-server.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "fhir-server.selectorLabels" . }}
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
{{- define "fhir-server.image" -}}
{{- $registry := .Values.image.registry | default ((.Values.global).imageRegistry) -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry .Values.image.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "fhir-server.imagePullSecrets" -}}
{{- $secrets := concat (.Values.imagePullSecrets | default list) ((.Values.global).imagePullSecrets | default list) | uniq -}}
{{- if $secrets }}
imagePullSecrets:
{{- range $secrets }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "fhir-server.scheduling" -}}
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
{{- define "fhir-server.database" -}}
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

{{- define "fhir-server.databaseSecretName" -}}
{{- $db := include "fhir-server.database" . | fromYaml -}}
{{- $db.existingSecret | default (printf "%s-db" (include "fhir-server.fullname" .)) -}}
{{- end -}}

{{/* Env vars for the database username and password, read from a Secret. */}}
{{- define "fhir-server.databaseCredentialEnv" -}}
{{- $db := include "fhir-server.database" .ctx | fromYaml -}}
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

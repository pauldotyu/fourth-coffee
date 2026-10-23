{{- define "fourth-coffee.version" -}}
{{- default .Chart.AppVersion .Values.image.tag -}}
{{- end -}}

{{- define "fourth-coffee.image" -}}
{{- $name := .Values.image.repository -}}
{{- if .Values.image.registry -}}
{{- $name = printf "%s/%s" .Values.image.registry $name -}}
{{- end -}}
{{- printf "%s:%s" $name (include "fourth-coffee.version" .) -}}
{{- end -}}

{{- define "fourth-coffee.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/part-of: fourth-coffee
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "fourth-coffee.secretName" -}}
{{- default .Values.secret.name .Values.secret.existingSecret -}}
{{- end -}}

{{- define "fourth-coffee.serviceAccountName" -}}
{{- .Values.workloadIdentity.serviceAccount.name -}}
{{- end -}}

{{- define "fourth-coffee.componentServiceAccountName" -}}
{{- if hasKey .root.Values.workloadIdentity.serviceAccounts .component.name -}}
{{- $account := get .root.Values.workloadIdentity.serviceAccounts .component.name -}}
{{- required (printf "workloadIdentity.serviceAccounts.%s.name is required" .component.name) $account.name -}}
{{- else -}}
{{- include "fourth-coffee.serviceAccountName" .root -}}
{{- end -}}
{{- end -}}

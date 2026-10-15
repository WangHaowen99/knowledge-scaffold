{{- define "modeling.name" -}}
{{- .Release.Name -}}
{{- end -}}
{{- define "modeling.pullSecrets" -}}
{{- range .Values.global.imageDelivery.pullSecretNames }}
- name: {{ . | quote }}
{{- end -}}
{{- end -}}
{{- define "modeling.arch" -}}
kubernetes.io/arch: {{ .Values.global.targetArch | quote }}
{{- end -}}
{{- define "modeling.authUrl" -}}
http://{{ .Values.externalAuth.serviceName }}.{{ .Values.externalAuth.namespace }}.svc.cluster.local:{{ .Values.externalAuth.servicePort }}
{{- end -}}
{{- define "modeling.consumerUrl" -}}
http://{{ .Values.externalConsumer.serviceName }}.{{ .Values.externalConsumer.namespace }}.svc.cluster.local:{{ .Values.externalConsumer.servicePort }}
{{- end -}}

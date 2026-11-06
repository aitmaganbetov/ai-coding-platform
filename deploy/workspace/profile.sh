# Shown in every new terminal of a workspace container.
case "$-" in *i*) ;; *) return ;; esac
cat <<'EOF'
AI Coding Platform — ваша рабочая область проекта (/workspace)

  claude                    Claude Code. Вход своей подпиской Pro/Max: команда /login
  codex                     Codex. Вход подпиской ChatGPT: codex login --device-auth
  git pull project <ветка>  забрать изменения из репозитория проекта
  git push origin <ветка>   отправить в GitHub (нужен ваш доступ к репозиторию)

Вход в Claude и ChatGPT хранится только в вашей домашней папке, другие его не видят.
EOF

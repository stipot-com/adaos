Кастомные пер-виртуалхост директивы nginx (по имени домена). Например, чтобы добавить security-заголовки/кэш:

* `vhost/app.inimatic.com`
* `vhost/api.inimatic.com`

Содержимое будет включено `nginx-proxy` в соответствующий `server {}`.

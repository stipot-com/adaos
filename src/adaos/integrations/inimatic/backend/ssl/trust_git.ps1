# создать файл known_hosts, если его нет
New-Item -ItemType Directory "$env:USERPROFILE\.ssh" -Force | Out-Null
# подтянуть ключ github.com и записать
ssh-keyscan github.com | Out-File -FilePath "$env:USERPROFILE\.ssh\known_hosts" -Append -Encoding ascii
# проверить доступ ключом
ssh -T -i c:/Users/inimatic/.ssh/adaos_root git@github.com

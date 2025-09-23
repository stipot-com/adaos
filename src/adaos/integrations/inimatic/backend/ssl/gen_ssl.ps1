# ssh-keygen -t ed25519 -C "adaos-root" -f .\adaos_root
Push-Location 'C:\git\MUIV\adaos\src\adaos\integrations\inimatic\backend\ssl'
# openssl genrsa -out ca.key 2048
@"
[ req ]
distinguished_name = dn
x509_extensions    = v3_ca
prompt             = no

[ dn ]
CN = AdaOS Dev CA

[ v3_ca ]
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid:always
basicConstraints = critical, CA:true, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
"@ | Set-Content .\mini-openssl.cnf -Encoding ascii

openssl req -new -x509 -key ca.key -sha256 -days 3650 `
  -config .\mini-openssl.cnf -out ca.crt
Pop-Location

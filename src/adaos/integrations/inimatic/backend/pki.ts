import forge from 'node-forge'

export type CertificateSubject = {
        commonName: string
        organizationName?: string
}

export type CertificateAuthorityOptions = {
        certPem: string
        keyPem: string
        defaultValidityDays?: number
}

export type IssueCertificateOptions = {
        csrPem: string
        subject: CertificateSubject
        validityDays?: number
}

export type IssueResult = {
        certificatePem: string
        serialNumber: string
}

export class CertificateAuthority {
        private readonly caCert: forge.pki.Certificate
        private readonly caKey: forge.pki.PrivateKey
        private readonly defaultValidityDays: number

        constructor(options: CertificateAuthorityOptions) {
                const { certPem, keyPem, defaultValidityDays } = options
                this.caCert = forge.pki.certificateFromPem(certPem)
                this.caKey = forge.pki.privateKeyFromPem(keyPem)
                this.defaultValidityDays = defaultValidityDays ?? 365
        }

        issueClientCertificate(options: IssueCertificateOptions): IssueResult {
                const { csrPem, subject } = options
                const csr = forge.pki.certificationRequestFromPem(csrPem)
                if (!csr.verify()) {
                        throw new Error('CSR verification failed')
                }

                const certificate = forge.pki.createCertificate()
                certificate.serialNumber = generateSerialNumber()
                const publicKey = csr.publicKey
                if (!publicKey) {
                        throw new Error('CSR does not contain a public key')
                }
                certificate.publicKey = publicKey

                const now = new Date()
                certificate.validity.notBefore = new Date(now.getTime() - 60_000)
                const days = options.validityDays ?? this.defaultValidityDays
                certificate.validity.notAfter = new Date(now.getTime() + days * 24 * 60 * 60 * 1000)

                const attrs: forge.pki.CertificateField[] = [
                        { name: 'commonName', value: subject.commonName },
                ]
                if (subject.organizationName) {
                        attrs.push({ name: 'organizationName', value: subject.organizationName })
                }
                certificate.setSubject(attrs)
                certificate.setIssuer(this.caCert.subject.attributes)

                certificate.setExtensions([
                        { name: 'basicConstraints', cA: false },
                        { name: 'extendedKeyUsage', clientAuth: true },
                        { name: 'keyUsage', digitalSignature: true, keyEncipherment: true },
                        {
                                name: 'subjectKeyIdentifier',
                                subjectKeyIdentifier: forge.pki.getPublicKeyFingerprint(publicKey, {
                                        type: 'SubjectPublicKeyInfo',
                                }) as forge.util.ByteStringBuffer,
                        },
                ])

                certificate.sign(this.caKey, forge.md.sha256.create())

                return {
                        certificatePem: forge.pki.certificateToPem(certificate),
                        serialNumber: certificate.serialNumber,
                }
        }
}

function generateSerialNumber(): string {
        const bytes = forge.random.getBytesSync(16)
        return forge.util.bytesToHex(bytes)
}

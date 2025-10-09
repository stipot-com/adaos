import forge from 'node-forge'

type ForgeCertificate = ReturnType<typeof forge.pki.certificateFromPem>
type ForgeRsaPrivateKey = forge.pki.rsa.PrivateKey
type ForgeCertificationRequest = ReturnType<typeof forge.pki.certificationRequestFromPem>
type ForgeSubjectAttribute = ForgeCertificate['subject']['attributes'][number]

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
	private readonly caCert: ForgeCertificate
	private readonly caKey: ForgeRsaPrivateKey
	private readonly defaultValidityDays: number
	private readonly caSubjectKeyIdBytes?: string // binary string

	constructor(options: CertificateAuthorityOptions) {
		const { certPem, keyPem, defaultValidityDays } = options

		this.caCert = forge.pki.certificateFromPem(certPem)

		const privateKey = forge.pki.privateKeyFromPem(keyPem)
		if (!isRsaPrivateKey(privateKey)) {
			throw new Error('Only RSA private keys are supported for the certificate authority')
		}
		this.caKey = privateKey
		this.defaultValidityDays = defaultValidityDays ?? 365

		// CA SKI для AKI
		try {
			const fp = forge.pki.getPublicKeyFingerprint(this.caCert.publicKey, {
				type: 'SubjectPublicKeyInfo',
			}) as forge.util.ByteStringBuffer
			this.caSubjectKeyIdBytes = fp.getBytes()
		} catch {
			/* optional */
		}
	}

	issueClientCertificate(options: IssueCertificateOptions): IssueResult {
		const { subject } = options
		const csrPem = normalizePem(options.csrPem)

		let csr: ForgeCertificationRequest
		try {
			csr = forge.pki.certificationRequestFromPem(csrPem)
		} catch {
			throw new Error('Failed to parse CSR PEM')
		}
		if (!csr.verify()) {
			throw new Error('CSR verification failed')
		}
		const publicKey = csr.publicKey
		if (!publicKey) {
			throw new Error('CSR does not contain a public key')
		}

                const cert = forge.pki.createCertificate()
                cert.serialNumber = generateSerialNumber()
                cert.publicKey = publicKey

                const now = Date.now()
                cert.validity.notBefore = new Date(now - 60_000)
                const days = options.validityDays ?? this.defaultValidityDays
                cert.validity.notAfter = new Date(now + days * 24 * 60 * 60 * 1000)

                const subjectAttrs = buildSubjectAttributes(csr, subject)
                cert.setSubject(subjectAttrs)
                cert.setIssuer(this.caCert.subject.attributes)

		// Расширения (SKI по hash, AKI из CA)
		const exts: any[] = [
			{ name: 'basicConstraints', cA: false },
			// В forge это extKeyUsage:
			{ name: 'extKeyUsage', clientAuth: true }, // можно добавить serverAuth: true при необходимости
			{ name: 'keyUsage', digitalSignature: true, keyEncipherment: true },
			{ name: 'subjectKeyIdentifier', hash: true },
		]
		if (this.caSubjectKeyIdBytes) {
			exts.push({ name: 'authorityKeyIdentifier', keyIdentifier: this.caSubjectKeyIdBytes })
		}
		cert.setExtensions(exts)

		try {
			cert.sign(this.caKey, forge.md.sha256.create())
		} catch (e: any) {
			throw new Error(`Certificate signing failed: ${e?.message || String(e)}`)
		}

		return {
			certificatePem: forge.pki.certificateToPem(cert),
			serialNumber: cert.serialNumber,
		}
	}
}

function normalizePem(input: string): string {
        return input.replace(/\r\n/g, '\n').trim() + '\n'
}

function buildSubjectAttributes(csr: ForgeCertificationRequest, subject: CertificateSubject): ForgeSubjectAttribute[] {
        const attrs: ForgeSubjectAttribute[] = Array.isArray(csr.subject?.attributes)
                ? csr.subject.attributes.map((attr) => ({ ...attr }))
                : []

        const upsert = (name: string, shortName: string, type: string, value?: string) => {
                if (!value) {
                        return
                }
                const existing = attrs.find(
                        (attr) => attr.type === type || attr.name === name || attr.shortName === shortName,
                )
                if (existing) {
                        existing.value = value
                        existing.name = name
                        existing.shortName = shortName
                        existing.type = type
                        return
                }
                attrs.push({ name, shortName, type, value })
        }

        upsert('commonName', 'CN', forge.pki.oids['commonName'], subject['commonName'])
        upsert(
                'organizationName',
                'O',
                forge.pki.oids['organizationName'],
                subject['organizationName'],
        )

        if (attrs.length === 0) {
                throw new Error('Certificate subject is empty')
        }

        return attrs
}

function generateSerialNumber(): string {
        const bytes = forge.random.getBytesSync(16)
        const hex = forge.util.bytesToHex(bytes)
        const first = (parseInt(hex.slice(0, 2), 16) & 0x7f).toString(16).padStart(2, '0')
        return first + hex.slice(2)
}

function isRsaPrivateKey(key: forge.pki.PrivateKey): key is ForgeRsaPrivateKey {
	return typeof (key as ForgeRsaPrivateKey).n !== 'undefined'
}

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
		const { csrPem, subject } = options;
		const csr = forge.pki.certificationRequestFromPem(csrPem);
		if (!csr.verify()) throw new Error('CSR verification failed');

		const certificate = forge.pki.createCertificate();
		const publicKey = csr.publicKey;
		if (!publicKey) {
			throw new Error('CSR does not contain a public key');
		}
		certificate.publicKey = publicKey;
		certificate.serialNumber = generateSerialNumber();
		const now = new Date();
		certificate.validity.notBefore = new Date(now.getTime() - 60_000);
		const days = options.validityDays ?? this.defaultValidityDays;
		certificate.validity.notAfter = new Date(now.getTime() + days * 86400_000);

		const { asn1 } = forge;
		const UTF8STRING_TAG: number = (asn1 as any)?.Type?.UTF8 ?? 12;
		const attrs: forge.pki.CertificateField[] = [
			{ name: 'commonName', value: subject.commonName, valueTagClass: UTF8STRING_TAG as any },
		];
		if (subject.organizationName) {
			attrs.push({ name: 'organizationName', value: subject.organizationName, valueTagClass: UTF8STRING_TAG as any });
		}
		certificate.setSubject(attrs as any);
		certificate.setIssuer(this.caCert.subject.attributes as any);

		certificate.setExtensions([
			{ name: 'basicConstraints', cA: false },
			{ name: 'keyUsage', digitalSignature: true, keyEncipherment: true },
			{ name: 'extKeyUsage', clientAuth: true }, // именно extKeyUsage
			{
				name: 'subjectKeyIdentifier',
				subjectKeyIdentifier: forge.pki.getPublicKeyFingerprint(certificate.publicKey, {
					type: 'SubjectPublicKeyInfo',
				}) as forge.util.ByteStringBuffer,
			},
		]);

		certificate.sign(this.caKey, forge.md.sha256.create());

		const pem = forge.pki.certificateToPem(certificate).replace(/\r\n/g, '\n').trim() + '\n';
		return { certificatePem: pem, serialNumber: certificate.serialNumber };
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

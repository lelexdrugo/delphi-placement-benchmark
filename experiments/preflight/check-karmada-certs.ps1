#requires -Version 7
<#
.SYNOPSIS
    Report the expiry of every certificate the Karmada control plane and the
    DELPHI components depend on, and fail when one is expired, drifted, or
    about to lapse.

.DESCRIPTION
    Written after the 2026-09-18 outage, in which every leaf certificate of the
    Karmada control plane expired simultaneously (all issued
    2025-09-18T16:49:55Z with one-year validity) and stopped a pre-registered
    acquisition at 368 of 504 executions. See
    [experiments/runtime/KARMADA-PKI-RENEWAL.md](../runtime/KARMADA-PKI-RENEWAL.md)
    for the repair procedure. This script is the prevention half: run it before
    a campaign and the next expiry is a five-minute maintenance window instead
    of a multi-day outage.

    SAFE TO RUN UNATTENDED: IT ONLY READS.

    Unlike its siblings in this directory, this script performs no writes at
    all. It issues `kubectl get` only, against `on-prem` and
    `unique-logical-entrypoint`, so read access to both is enough.
    It creates no pods, patches nothing, and never prints key material.

    It checks four classes of material:

    1. Leaf and CA certificates in the host-cluster secrets `karmada-cert`,
       `etcd-cert`, `karmada-webhook-cert`, `root-ca` and `webhook-server-tls`.
    2. Client certificates embedded in the `kubeconfig`, `karmada-kubeconfig`
       and member kubeconfig secrets, on both the host cluster and the Karmada
       control plane.
    3. Duplicate consistency: `etcd-cert` carries its own copy of
       `etcd-ca.crt` and `etcd-server.crt`. A renewal that patches one secret
       and not the other leaves etcd trusting a certificate the apiserver no
       longer presents, which is silent until the next etcd restart.
    4. Webhook trust on the Karmada control plane. These webhook
       configurations live in the Karmada aggregated API, not on the host
       cluster. cert-manager's cainjector runs on the host cluster and watches
       the host cluster's admission configurations, so it cannot see them: no
       `cert-manager.io/inject-ca-from` annotation can work at this boundary,
       which is why the bundles are hand-copied and why nothing renews them.

       Expiry dates alone do not catch the failure that matters. The check
       therefore tests the relation between the bundle and the leaf it must
       validate, reporting ISSUER-ABSENT when the CA that signed the serving
       certificate is not in the bundle, and ANCHOR-EXPIRY when the pinned CA
       expires before the leaf it validates -- which breaks admission on the
       CA's date, not the leaf's. With `failurePolicy: Fail` on CREATE of
       `batch/v1` Jobs, that rejects every experiment Job.

       The per-certificate MISMATCH comparison is retained as an early signal
       of drift. It is per certificate, not per bundle: a caBundle that
       deliberately carries both the outgoing and incoming CA during an
       overlapped rotation reports MISMATCH for the old one and OK for the
       new one. That is expected during a healthy roll, and the ISSUER-ABSENT
       and ANCHOR-EXPIRY verdicts are the ones to act on.

    Certificates managed by cert-manager are reported as MANAGED with their
    scheduled renewal date rather than counted against the threshold: they
    renew themselves, and flagging them trains the reader to ignore the
    output. A MANAGED certificate that has actually expired is still CRITICAL.

    The karmada-agent client certificate of a Pull-mode member lives in a
    kubeconfig on the node itself and is not readable from the control plane.
    Those rows are reported explicitly as NODE-LOCAL rather than omitted — a
    check that quietly skips the material that actually broke is worse than no
    check at all.

.PARAMETER HostContext
    Context of the cluster hosting the Karmada control plane. Default `on-prem`.

.PARAMETER KarmadaContext
    Context of the Karmada control plane itself. Default
    `unique-logical-entrypoint`.

.PARAMETER Namespace
    Namespace holding the control-plane secrets. Default `karmada-system`.

.PARAMETER WarnDays
    Threshold in days below which an unmanaged certificate is a warning.
    Default 30.

.PARAMETER KubectlPath
    kubectl binary to use. Default `kubectl` from PATH. Follows the precedent
    set by the campaign driver.

.PARAMETER TimeoutSeconds
    Per-request kubectl timeout. Default 15s.

.PARAMETER Json
    Emit a single JSON document on stdout instead of the human-readable table.

.OUTPUTS
    A table (or JSON with -Json) of one row per certificate: source, subject,
    not-after, days remaining, and status. No certificate body, private key or
    kubeconfig content is ever printed.

    Exit codes:
      0  every certificate healthy.
      1  CRITICAL  - at least one certificate expired, or a caBundle pins a
                     superseded CA, or a duplicated certificate has drifted.
      2  WARNING   - an unmanaged certificate is under -WarnDays, or an
                     expired credential that nothing references is still
                     lying around (STALE).
      3  INDETERMINATE - a cluster was unreachable or an expected secret was
                     absent, so the answer is not trustworthy.
    Precedence when several apply: 1 beats 3 beats 2.

.EXAMPLE
    .\experiments\preflight\check-karmada-certs.ps1

    Prints the full inventory and exits non-zero if anything needs attention.

.EXAMPLE
    .\experiments\preflight\check-karmada-certs.ps1 -WarnDays 60 -Json

    Machine-readable form with a wider horizon, for a scheduled check.
#>

[CmdletBinding()]
param(
    [Parameter()]
    [string]$HostContext = 'on-prem',

    [Parameter()]
    [string]$KarmadaContext = 'unique-logical-entrypoint',

    [Parameter()]
    [string]$Namespace = 'karmada-system',

    [Parameter()]
    [ValidateRange(1, 3650)]
    [int]$WarnDays = 30,

    [Parameter()]
    [string]$KubectlPath = 'kubectl',

    [Parameter()]
    [ValidateRange(1, 600)]
    [int]$TimeoutSeconds = 15,

    [Parameter()]
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:Rows = [System.Collections.Generic.List[object]]::new()
$script:Notes = [System.Collections.Generic.List[string]]::new()
$script:Now = [DateTime]::UtcNow

# ----------------------------------------------------------------------------
# Inventory. Declared as data so that what is covered is auditable by reading
# the script rather than by running it.
# ----------------------------------------------------------------------------

$CertSecrets = @(
    @{ Secret = 'karmada-cert'
       Role   = 'Karmada control-plane PKI'
       Keys   = @('ca.crt', 'etcd-ca.crt', 'front-proxy-ca.crt', 'apiserver.crt',
                  'etcd-server.crt', 'etcd-client.crt', 'front-proxy-client.crt', 'karmada.crt') }
    @{ Secret = 'etcd-cert'
       Role   = 'etcd StatefulSet (second copy)'
       Keys   = @('etcd-ca.crt', 'etcd-server.crt') }
    @{ Secret = 'karmada-webhook-cert'
       Role   = 'karmada-webhook serving'
       Keys   = @('tls.crt') }
    @{ Secret = 'root-ca'
       Role   = 'cert-manager CA behind the DELPHI sidecar injector'
       Keys   = @('tls.crt') }
    @{ Secret = 'webhook-server-tls'
       Role   = 'job-scheduler-controller webhook serving'
       Keys   = @('tls.crt', 'ca.crt') }
)

# Which serving certificate each control-plane webhook actually presents, and
# which secret issues it. Declared rather than inferred: the karmada-webhook
# certificate carries no SAN, so it cannot be matched to its host by name.
$WebhookServingCerts = @(
    @{ Host      = 'webhook-service.karmada-system.svc'
       Secret    = 'webhook-server-tls'; Key = 'tls.crt'
       Component = 'job-scheduler-controller (DELPHI sidecar injector)' }
    @{ Host      = 'karmada-webhook.karmada-system.svc'
       Secret    = 'karmada-webhook-cert'; Key = 'tls.crt'
       Component = 'karmada-webhook' }
)

$KubeconfigSecrets = @(
    @{ Secret = 'kubeconfig';                  Key = 'kubeconfig';                  Role = 'in-cluster admin kubeconfig' }
    @{ Secret = 'karmada-kubeconfig';          Key = 'config';                      Role = 'decision-maker / controller kubeconfig' }
    @{ Secret = 'public-cloud-sa2-kubeconfig'; Key = 'public-cloud-sa2-kubeconfig'; Role = 'scheduler-estimator member kubeconfig' }
)

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

function Add-Row {
    param(
        [string]$Source,
        [string]$Role,
        [string]$Subject,
        # Deliberately untyped: PowerShell does not bind [Nullable[DateTime]]
        # as a nullable, so an explicit $null check is the only reliable form.
        $NotAfter = $null,
        [string]$Status,
        [string]$Detail = ''
    )
    $days = $null
    $notAfterText = $null
    if ($null -ne $NotAfter) {
        $dt = [DateTime]$NotAfter
        $days = [int][math]::Floor(($dt - $script:Now).TotalDays)
        $notAfterText = $dt.ToString('yyyy-MM-ddTHH:mm:ssZ')
    }
    $script:Rows.Add([ordered]@{
        source         = $Source
        role           = $Role
        subject        = $Subject
        not_after      = $notAfterText
        days_remaining = $days
        status         = $Status
        detail         = $Detail
    })
}

function Invoke-KubectlJson {
    <#
        Returns the parsed JSON document, or $null when the resource is absent
        or the cluster is unreachable. Never throws: an unreachable control
        plane must degrade to a clear message, not a stack trace.
    #>
    param(
        [Parameter(Mandatory)][string]$Context,
        [Parameter(Mandatory)][string[]]$KubectlArgs
    )
    $all = @('--context', $Context, "--request-timeout=$($TimeoutSeconds)s") + $KubectlArgs + @('-o', 'json')
    try {
        $out = & $KubectlPath @all 2>&1
        $code = $LASTEXITCODE
    }
    catch {
        $script:Notes.Add("kubectl could not be executed at '$KubectlPath': $($_.Exception.Message)")
        return $null
    }
    if ($code -ne 0) {
        $text = ($out | Out-String).Trim()
        $first = ($text -split "`n" | Select-Object -First 1)
        if ($text -match 'NotFound|not found') {
            $script:Notes.Add("absent: $($KubectlArgs -join ' ') on $Context")
        }
        else {
            $script:Notes.Add("unreachable or denied: $($KubectlArgs -join ' ') on $Context :: $first")
        }
        return $null
    }
    try { return ($out | Out-String | ConvertFrom-Json) }
    catch {
        $script:Notes.Add("unparseable JSON from: $($KubectlArgs -join ' ') on $Context")
        return $null
    }
}

function Get-DataValue {
    <# Decode one base64 entry of a secret's .data map, or $null if absent. #>
    param($Secret, [string]$Key)
    if ($null -eq $Secret) { return $null }
    $data = $Secret.PSObject.Properties['data']
    if ($null -eq $data -or $null -eq $data.Value) { return $null }
    $prop = $data.Value.PSObject.Properties[$Key]
    if ($null -eq $prop -or [string]::IsNullOrWhiteSpace($prop.Value)) { return $null }
    try { return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($prop.Value)) }
    catch { return $null }
}

function ConvertFrom-PemBundle {
    <# Every certificate in a PEM blob, in order. Ignores keys entirely. #>
    param([string]$Pem)
    $result = [System.Collections.Generic.List[object]]::new()
    if ([string]::IsNullOrWhiteSpace($Pem)) { return $result }
    foreach ($m in [regex]::Matches($Pem, '(?s)-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----')) {
        try { $result.Add([System.Security.Cryptography.X509Certificates.X509Certificate2]::CreateFromPem($m.Value)) }
        catch { }
    }
    # Comma prevents PowerShell from unrolling a single-element list into a
    # scalar, which would lose .Count under Set-StrictMode.
    return , $result
}

function Get-Ski {
    <# Subject key identifier as lowercase hex, or '' when absent. #>
    param($Cert)
    foreach ($e in $Cert.Extensions) {
        if ($e.Oid.Value -ne '2.5.29.14') { continue }
        try {
            $ext = [System.Security.Cryptography.X509Certificates.X509SubjectKeyIdentifierExtension]::new($e, $false)
            return $ext.SubjectKeyIdentifier.ToLowerInvariant()
        }
        catch {
            # DER fallback: the extension value is an OCTET STRING wrapping the id.
            $raw = $e.RawData
            if ($raw.Length -gt 2 -and $raw[0] -eq 0x04) {
                return (($raw[2..($raw.Length - 1)] | ForEach-Object { $_.ToString('x2') }) -join '')
            }
        }
    }
    return ''
}

function Get-Aki {
    <#
        Authority key identifier as lowercase hex, or '' when absent. Parsed
        from DER rather than from Format(), whose text varies by platform:
        AuthorityKeyIdentifier ::= SEQUENCE { [0] keyIdentifier OPTIONAL, ... }
    #>
    param($Cert)
    foreach ($e in $Cert.Extensions) {
        if ($e.Oid.Value -ne '2.5.29.35') { continue }
        $raw = $e.RawData
        if ($raw.Length -lt 4 -or $raw[0] -ne 0x30) { return '' }
        $i = 1
        if ($raw[$i] -band 0x80) { $i += 1 + ($raw[$i] -band 0x7f) } else { $i += 1 }
        if ($i + 1 -ge $raw.Length -or $raw[$i] -ne 0x80) { return '' }
        $len = $raw[$i + 1]
        $start = $i + 2
        if ($len -le 0 -or $start + $len -gt $raw.Length) { return '' }
        return (($raw[$start..($start + $len - 1)] | ForEach-Object { $_.ToString('x2') }) -join '')
    }
    return ''
}

function Get-ClientCertFromKubeconfig {
    <#
        The embedded client certificate of a kubeconfig. Only the certificate
        is decoded; client-key-data is never read, decoded or printed.
    #>
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $m = [regex]::Match($Text, 'client-certificate-data:\s*([A-Za-z0-9+/=]+)')
    if (-not $m.Success) { return $null }
    try {
        $pem = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($m.Groups[1].Value))
        return [System.Security.Cryptography.X509Certificates.X509Certificate2]::CreateFromPem($pem)
    }
    catch { return $null }
}

function Get-CertManagerInfo {
    <#
        cert-manager stamps the secrets it owns. Returns the renewal time when
        the owning Certificate is healthy, else $null.
    #>
    param($Secret, $Certificates)
    if ($null -eq $Secret -or $null -eq $Certificates) { return $null }
    $ann = $Secret.PSObject.Properties['metadata'].Value.PSObject.Properties['annotations']
    if ($null -eq $ann -or $null -eq $ann.Value) { return $null }
    $nameProp = $ann.Value.PSObject.Properties['cert-manager.io/certificate-name']
    if ($null -eq $nameProp) { return $null }
    $certName = [string]$nameProp.Value
    foreach ($c in $Certificates) {
        if ($c.metadata.name -eq $certName) {
            $status = $c.PSObject.Properties['status']
            $renewal = $null
            if ($null -ne $status -and $null -ne $status.Value) {
                $rp = $status.Value.PSObject.Properties['renewalTime']
                if ($null -ne $rp -and $null -ne $rp.Value) {
                    # ConvertFrom-Json hands back a DateTime; normalise to ISO
                    # so the output does not vary with the host locale.
                    try { $renewal = ([DateTime]$rp.Value).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
                    catch { $renewal = [string]$rp.Value }
                }
            }
            return [ordered]@{ name = $certName; renewal_time = $renewal }
        }
    }
    return [ordered]@{ name = $certName; renewal_time = $null }
}

function Get-ExpiryStatus {
    <# Classify one certificate. Managed certificates renew themselves. #>
    param([DateTime]$NotAfter, $Managed)
    $days = [math]::Floor(($NotAfter - $script:Now).TotalDays)
    if ($days -lt 0) { return 'EXPIRED' }
    if ($null -ne $Managed) { return 'MANAGED' }
    if ($days -lt $WarnDays) { return 'EXPIRING' }
    return 'OK'
}

# ----------------------------------------------------------------------------
# 1. Host-cluster certificate secrets
# ----------------------------------------------------------------------------

$certManagerCerts = $null
$cmDoc = Invoke-KubectlJson -Context $HostContext -KubectlArgs @('get', 'certificates.cert-manager.io', '-n', $Namespace)
if ($null -ne $cmDoc -and $cmDoc.PSObject.Properties['items']) { $certManagerCerts = $cmDoc.items }

$secretCache = @{}
foreach ($target in $CertSecrets) {
    $name = $target.Secret
    $secret = Invoke-KubectlJson -Context $HostContext -KubectlArgs @('get', 'secret', $name, '-n', $Namespace)
    $secretCache[$name] = $secret
    if ($null -eq $secret) {
        Add-Row -Source "$name" -Role $target.Role -Subject '-' -NotAfter $null `
                -Status 'UNREADABLE' -Detail "secret absent or not readable on $HostContext/$Namespace"
        continue
    }
    $managed = Get-CertManagerInfo -Secret $secret -Certificates $certManagerCerts
    foreach ($key in $target.Keys) {
        $pem = Get-DataValue -Secret $secret -Key $key
        if ($null -eq $pem) {
            Add-Row -Source "$name/$key" -Role $target.Role -Subject '-' -NotAfter $null `
                    -Status 'UNREADABLE' -Detail 'key absent from secret'
            continue
        }
        $certs = @(ConvertFrom-PemBundle -Pem $pem)
        if ($certs.Count -eq 0) {
            Add-Row -Source "$name/$key" -Role $target.Role -Subject '-' -NotAfter $null `
                    -Status 'UNREADABLE' -Detail 'no parseable certificate'
            continue
        }
        foreach ($c in $certs) {
            $status = Get-ExpiryStatus -NotAfter $c.NotAfter.ToUniversalTime() -Managed $managed
            $detail = ''
            if ($null -ne $managed) {
                $detail = "cert-manager/$($managed.name)"
                if ($managed.renewal_time) { $detail += " renews $($managed.renewal_time)" }
            }
            Add-Row -Source "$name/$key" -Role $target.Role -Subject $c.Subject `
                    -NotAfter $c.NotAfter.ToUniversalTime() -Status $status -Detail $detail
        }
    }
}

# ----------------------------------------------------------------------------
# 2. Duplicate consistency between karmada-cert and etcd-cert
# ----------------------------------------------------------------------------

$dupKeys = @('etcd-ca.crt', 'etcd-server.crt')
foreach ($key in $dupKeys) {
    $a = @(ConvertFrom-PemBundle -Pem (Get-DataValue -Secret $secretCache['karmada-cert'] -Key $key))
    $b = @(ConvertFrom-PemBundle -Pem (Get-DataValue -Secret $secretCache['etcd-cert'] -Key $key))
    if ($a.Count -eq 0 -or $b.Count -eq 0) {
        Add-Row -Source "duplicate:$key" -Role 'karmada-cert vs etcd-cert' -Subject '-' -NotAfter $null `
                -Status 'UNREADABLE' -Detail 'one of the two copies could not be read'
        continue
    }
    if ($a[0].Thumbprint -eq $b[0].Thumbprint) {
        Add-Row -Source "duplicate:$key" -Role 'karmada-cert vs etcd-cert' -Subject $a[0].Subject `
                -NotAfter $a[0].NotAfter.ToUniversalTime() -Status 'OK' -Detail 'both copies identical'
    }
    else {
        Add-Row -Source "duplicate:$key" -Role 'karmada-cert vs etcd-cert' -Subject $a[0].Subject `
                -NotAfter $a[0].NotAfter.ToUniversalTime() -Status 'MISMATCH' `
                -Detail 'karmada-cert and etcd-cert hold different certificates for this key'
    }
}

# ----------------------------------------------------------------------------
# 3. Kubeconfig-embedded client certificates (host cluster)
# ----------------------------------------------------------------------------

foreach ($target in $KubeconfigSecrets) {
    $secret = Invoke-KubectlJson -Context $HostContext -KubectlArgs @('get', 'secret', $target.Secret, '-n', $Namespace)
    if ($null -eq $secret) {
        Add-Row -Source $target.Secret -Role $target.Role -Subject '-' -NotAfter $null `
                -Status 'UNREADABLE' -Detail "secret absent or not readable on $HostContext/$Namespace"
        continue
    }
    $text = Get-DataValue -Secret $secret -Key $target.Key
    $cert = Get-ClientCertFromKubeconfig -Text $text
    if ($null -eq $cert) {
        Add-Row -Source "$($target.Secret)/$($target.Key)" -Role $target.Role -Subject '-' -NotAfter $null `
                -Status 'UNREADABLE' -Detail 'no embedded client certificate (token-based or unreadable)'
        continue
    }
    Add-Row -Source "$($target.Secret)/$($target.Key)" -Role $target.Role -Subject $cert.Subject `
            -NotAfter $cert.NotAfter.ToUniversalTime() `
            -Status (Get-ExpiryStatus -NotAfter $cert.NotAfter.ToUniversalTime() -Managed $null) `
            -Detail 'embedded client certificate'
}

# ----------------------------------------------------------------------------
# 4. Member clusters: the credential Karmada actually uses
#
# Every Cluster names its credential in .spec.secretRef. Those secrets hold a
# caBundle plus a service-account token, and they -- not the `<name>-kubeconfig`
# secrets left behind by `karmadactl join` -- are the live authentication path.
# Knowing which is which is what keeps this check from crying wolf.
# ----------------------------------------------------------------------------

$clusters = Invoke-KubectlJson -Context $KarmadaContext -KubectlArgs @('get', 'clusters.cluster.karmada.io')
$referencedSecrets = @{}
if ($null -ne $clusters -and $clusters.PSObject.Properties['items']) {
    foreach ($cl in $clusters.items) {
        $ref = $cl.spec.PSObject.Properties['secretRef']
        if ($null -eq $ref -or $null -eq $ref.Value) { continue }
        $refNs = [string]$ref.Value.namespace
        $refName = [string]$ref.Value.name
        $referencedSecrets["$refNs/$refName"] = $cl.metadata.name

        $memberSecret = Invoke-KubectlJson -Context $KarmadaContext -KubectlArgs @('get', 'secret', $refName, '-n', $refNs)
        if ($null -eq $memberSecret) {
            Add-Row -Source "$refNs/$refName" -Role "live credential for $($cl.metadata.name)" -Subject '-' `
                    -Status 'UNREADABLE' -Detail 'secret named by Cluster.spec.secretRef is not readable'
            continue
        }
        $bundle = @(ConvertFrom-PemBundle -Pem (Get-DataValue -Secret $memberSecret -Key 'caBundle'))
        foreach ($c in $bundle) {
            Add-Row -Source "$refNs/$refName caBundle" -Role "live credential for $($cl.metadata.name)" `
                    -Subject $c.Subject -NotAfter $c.NotAfter.ToUniversalTime() `
                    -Status (Get-ExpiryStatus -NotAfter $c.NotAfter.ToUniversalTime() -Managed $null) `
                    -Detail "referenced by Cluster/$($cl.metadata.name) (syncMode=$($cl.spec.syncMode))"
        }
    }
}

# ----------------------------------------------------------------------------
# 5. Kubeconfig secrets on the Karmada control plane itself
# ----------------------------------------------------------------------------

$cpSecrets = Invoke-KubectlJson -Context $KarmadaContext -KubectlArgs @('get', 'secrets', '-n', $Namespace)
if ($null -ne $cpSecrets -and $cpSecrets.PSObject.Properties['items']) {
    foreach ($s in $cpSecrets.items) {
        if ($s.metadata.name -notlike '*kubeconfig*') { continue }
        $dataProp = $s.PSObject.Properties['data']
        if ($null -eq $dataProp -or $null -eq $dataProp.Value) { continue }
        foreach ($p in $dataProp.Value.PSObject.Properties) {
            $text = Get-DataValue -Secret $s -Key $p.Name
            $cert = Get-ClientCertFromKubeconfig -Text $text
            if ($null -eq $cert) { continue }
            $notAfter = $cert.NotAfter.ToUniversalTime()
            $status = Get-ExpiryStatus -NotAfter $notAfter -Managed $null
            $isReferenced = $referencedSecrets.ContainsKey("$Namespace/$($s.metadata.name)")
            $detail = 'embedded client certificate'
            if ($isReferenced) {
                $detail += "; referenced by Cluster/$($referencedSecrets["$Namespace/$($s.metadata.name)"])"
            }
            elseif ($status -eq 'EXPIRED') {
                # Not named by any Cluster.spec.secretRef, so nothing
                # authenticates with it. Real debt, but not an outage.
                $status = 'STALE'
                $detail += '; not referenced by any Cluster.spec.secretRef, so not on the live path. Left over from karmadactl join: renew or delete.'
            }
            Add-Row -Source "$($s.metadata.name)/$($p.Name)" -Role 'kubeconfig on control plane' `
                    -Subject $cert.Subject -NotAfter $notAfter -Status $status -Detail $detail
        }
    }
}

# ----------------------------------------------------------------------------
# 6. Webhook caBundles on the Karmada control plane
#
# These objects live in the Karmada aggregated API, not on the host cluster.
# cert-manager's cainjector runs on the host cluster and watches the host
# cluster's admission configurations, so it cannot see them: no
# `cert-manager.io/inject-ca-from` annotation can work here, which is why the
# bundles are hand-copied. Nothing renews them. Checking that is the point of
# this section.
#
# Expiry dates alone do not catch the failure. What matters is the relation
# between the bundle and the leaf it must validate:
#   * the leaf's issuer must be present in the bundle, and
#   * every anchor that validates the leaf must outlive the leaf.
# ----------------------------------------------------------------------------

# Current CA material, by subject, as held in the issuing secrets.
$currentCAs = @{}
foreach ($pair in @(@('karmada-cert', 'ca.crt'), @('root-ca', 'tls.crt'))) {
    $certs = @(ConvertFrom-PemBundle -Pem (Get-DataValue -Secret $secretCache[$pair[0]] -Key $pair[1]))
    if ($certs.Count -gt 0) { $currentCAs[$certs[0].Subject] = $certs[0] }
}

foreach ($kind in @('mutatingwebhookconfigurations', 'validatingwebhookconfigurations')) {
    $doc = Invoke-KubectlJson -Context $KarmadaContext -KubectlArgs @('get', $kind)
    if ($null -eq $doc -or -not $doc.PSObject.Properties['items']) { continue }
    foreach ($cfg in $doc.items) {
        $seen = @{}
        foreach ($w in $cfg.webhooks) {
            $cc = $w.clientConfig
            $target = if ($cc.PSObject.Properties['service'] -and $null -ne $cc.service) {
                "$($cc.service.namespace)/$($cc.service.name)"
            }
            elseif ($cc.PSObject.Properties['url'] -and $cc.url) { [string]$cc.url } else { '' }
            # Only our own control-plane webhooks are in scope.
            if ($target -notmatch $Namespace) { continue }
            if (-not $cc.PSObject.Properties['caBundle'] -or [string]::IsNullOrWhiteSpace($cc.caBundle)) {
                Add-Row -Source "$($cfg.metadata.name)" -Role 'webhook caBundle' -Subject '-' -NotAfter $null `
                        -Status 'UNREADABLE' -Detail "no caBundle on webhook $($w.name)"
                continue
            }
            if ($seen.ContainsKey([string]$cc.caBundle)) { continue }
            $seen[[string]$cc.caBundle] = $true
            $bundlePem = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([string]$cc.caBundle))
            $bundleCerts = @(ConvertFrom-PemBundle -Pem $bundlePem)

            $handMaintained = 'hand-copied: cainjector runs on the host cluster and cannot see objects in the Karmada aggregated API, so nothing renews this bundle.'

            foreach ($c in $bundleCerts) {
                $status = Get-ExpiryStatus -NotAfter $c.NotAfter.ToUniversalTime() -Managed $null
                $detail = "policy=$($w.failurePolicy) target=$target"
                if ($currentCAs.ContainsKey($c.Subject)) {
                    if ($currentCAs[$c.Subject].Thumbprint -ne $c.Thumbprint) {
                        $status = 'MISMATCH'
                        $detail = "pins a superseded '$($c.Subject)'; the issuing secret now holds a different CA. $detail"
                    }
                    else { $detail = "pins the current CA. $detail" }
                }
                else { $detail = "anchor not found in any scanned secret. $detail" }
                Add-Row -Source "$($cfg.metadata.name)" -Role 'webhook caBundle' -Subject $c.Subject `
                        -NotAfter $c.NotAfter.ToUniversalTime() -Status $status -Detail "$detail $handMaintained"
            }

            # ---- the relation that actually decides whether admission works ----
            $serving = $WebhookServingCerts | Where-Object { $target -match [regex]::Escape($_.Host) } | Select-Object -First 1
            if ($null -eq $serving) { continue }
            $leafCerts = @(ConvertFrom-PemBundle -Pem (Get-DataValue -Secret $secretCache[$serving.Secret] -Key $serving.Key))
            if ($leafCerts.Count -eq 0) {
                Add-Row -Source "$($cfg.metadata.name) -> leaf" -Role 'webhook trust chain' -Subject '-' -NotAfter $null `
                        -Status 'UNREADABLE' -Detail "serving certificate $($serving.Secret)/$($serving.Key) not readable"
                continue
            }
            $leaf = $leafCerts[0]
            $leafNotAfter = $leaf.NotAfter.ToUniversalTime()
            $leafAki = Get-Aki -Cert $leaf
            $anchors = @($bundleCerts | Where-Object { (Get-Ski -Cert $_) -eq $leafAki -and $leafAki -ne '' })

            if ($anchors.Count -eq 0) {
                Add-Row -Source "$($cfg.metadata.name) -> leaf" -Role 'webhook trust chain' -Subject $leaf.Subject `
                        -NotAfter $leafNotAfter -Status 'ISSUER-ABSENT' `
                        -Detail ("the CA that signed $($serving.Secret)/$($serving.Key) is not in this caBundle, " +
                                 "so $($serving.Component) admission fails. policy=$($w.failurePolicy). $handMaintained")
                continue
            }

            # The anchor must outlive the leaf it validates, or admission breaks
            # on the anchor's expiry date rather than the leaf's.
            $earliest = ($anchors | Sort-Object { $_.NotAfter } | Select-Object -First 1)
            $anchorNotAfter = $earliest.NotAfter.ToUniversalTime()
            if ($anchorNotAfter -lt $leafNotAfter) {
                Add-Row -Source "$($cfg.metadata.name) -> leaf" -Role 'webhook trust chain' -Subject $leaf.Subject `
                        -NotAfter $anchorNotAfter -Status 'ANCHOR-EXPIRY' `
                        -Detail ("the pinned CA expires $($anchorNotAfter.ToString('yyyy-MM-dd')), before the leaf it validates " +
                                 "($($leafNotAfter.ToString('yyyy-MM-dd'))), so $($serving.Component) admission breaks on the CA date. " +
                                 "policy=$($w.failurePolicy). $handMaintained")
            }
            else {
                Add-Row -Source "$($cfg.metadata.name) -> leaf" -Role 'webhook trust chain' -Subject $leaf.Subject `
                        -NotAfter $leafNotAfter -Status 'OK' `
                        -Detail "bundle contains the leaf's issuer and outlives it. policy=$($w.failurePolicy)"
            }
        }
    }
}

# ----------------------------------------------------------------------------
# 7. Pull-mode agent certificates: node-local, declared not omitted
# ----------------------------------------------------------------------------

if ($null -ne $clusters -and $clusters.PSObject.Properties['items']) {
    foreach ($cl in $clusters.items) {
        if ($cl.spec.syncMode -ne 'Pull') { continue }
        $ready = 'Unknown'
        if ($cl.PSObject.Properties['status'] -and $cl.status.PSObject.Properties['conditions']) {
            foreach ($cond in $cl.status.conditions) { if ($cond.type -eq 'Ready') { $ready = [string]$cond.status } }
        }
        Add-Row -Source "$($cl.metadata.name) karmada-agent" -Role 'Pull-mode agent client certificate' `
                -Subject '(not readable from the control plane)' -NotAfter $null -Status 'NODE-LOCAL' `
                -Detail "kubeconfig lives on the node; cluster Ready=$ready. Check on the node itself."
    }
}

# ----------------------------------------------------------------------------
# Verdict
# ----------------------------------------------------------------------------

$critical = @($script:Rows | Where-Object { $_.status -in @('EXPIRED', 'MISMATCH', 'ISSUER-ABSENT', 'ANCHOR-EXPIRY') })
$warning = @($script:Rows | Where-Object { $_.status -in @('EXPIRING', 'STALE') })
$unknown = @($script:Rows | Where-Object { $_.status -in @('UNREADABLE', 'NODE-LOCAL') })

$exitCode = 0
if ($critical.Count -gt 0) { $exitCode = 1 }
elseif ($unknown.Count -gt 0) { $exitCode = 3 }
elseif ($warning.Count -gt 0) { $exitCode = 2 }

if ($Json) {
    $payload = [ordered]@{
        schema_version   = 1
        generated_at_utc = $script:Now.ToString('yyyy-MM-ddTHH:mm:ssZ')
        host_context     = $HostContext
        karmada_context  = $KarmadaContext
        namespace        = $Namespace
        warn_days        = $WarnDays
        exit_code        = $exitCode
        counts           = [ordered]@{
            total = $script:Rows.Count; critical = $critical.Count
            warning = $warning.Count; indeterminate = $unknown.Count
        }
        notes            = @($script:Notes)
        certificates     = @($script:Rows)
    }
    $payload | ConvertTo-Json -Depth 8
    exit $exitCode
}

function Get-StatusColor([string]$Status) {
    switch ($Status) {
        'OK'         { 'Green' }
        'MANAGED'    { 'DarkGray' }
        'EXPIRING'   { 'Yellow' }
        'STALE'      { 'Yellow' }
        'EXPIRED'        { 'Red' }
        'MISMATCH'       { 'Red' }
        'ISSUER-ABSENT'  { 'Red' }
        'ANCHOR-EXPIRY'  { 'Red' }
        'UNREADABLE' { 'Magenta' }
        'NODE-LOCAL' { 'Cyan' }
        default      { 'Gray' }
    }
}

Write-Host "==> Karmada certificate status  ($HostContext / $KarmadaContext, ns=$Namespace, threshold=${WarnDays}d)" -ForegroundColor Cyan
Write-Host ("    as of {0:yyyy-MM-dd HH:mm:ss} UTC" -f $script:Now) -ForegroundColor DarkGray
Write-Host ''
Write-Host ("    {0,-46} {1,-42} {2,-12} {3,6}  {4}" -f 'SOURCE', 'SUBJECT', 'NOT-AFTER', 'DAYS', 'STATUS') -ForegroundColor DarkGray

foreach ($r in $script:Rows) {
    $na = if ($r.not_after) { ([DateTime]::Parse($r.not_after)).ToString('yyyy-MM-dd') } else { '-' }
    $dd = if ($null -ne $r.days_remaining) { [string]$r.days_remaining } else { '-' }
    $src = $r.source.Substring(0, [Math]::Min(46, $r.source.Length))
    $subj = $r.subject.Substring(0, [Math]::Min(42, $r.subject.Length))
    Write-Host ("    {0,-46} {1,-42} {2,-12} {3,6}  " -f $src, $subj, $na, $dd) -NoNewline
    Write-Host $r.status -ForegroundColor (Get-StatusColor $r.status)
    if ($r.detail) { Write-Host "        $($r.detail)" -ForegroundColor DarkGray }
}

Write-Host ''
foreach ($n in $script:Notes) { Write-Host "    note: $n" -ForegroundColor DarkGray }
if ($script:Notes.Count -gt 0) { Write-Host '' }

if ($critical.Count -gt 0) {
    Write-Host "==> CRITICAL: $($critical.Count) certificate(s) expired or drifted" -ForegroundColor Red
    foreach ($r in $critical) { Write-Host "      $($r.source) [$($r.status)]" -ForegroundColor Red }
}
if ($warning.Count -gt 0) {
    Write-Host "==> WARNING: $($warning.Count) item(s) expiring within ${WarnDays} days or stale" -ForegroundColor Yellow
    foreach ($r in $warning) { Write-Host "      $($r.source) [$($r.status)] ($($r.days_remaining)d)" -ForegroundColor Yellow }
}
if ($unknown.Count -gt 0) {
    Write-Host "==> INDETERMINATE: $($unknown.Count) item(s) not readable from here" -ForegroundColor Magenta
    foreach ($r in $unknown) { Write-Host "      $($r.source) [$($r.status)]" -ForegroundColor Magenta }
}
if ($exitCode -eq 0) { Write-Host '==> all certificates healthy' -ForegroundColor Green }

Write-Host ("==> exit {0}" -f $exitCode) -ForegroundColor DarkGray
exit $exitCode

# Choosing a replica with discovery

Sage publishes the names belonging to a service. Your client chooses a replica and sends application requests
directly to it. Discovery does not forward application traffic or choose a replica for the client.

## Frontend and backend setup

For a frontend at `https://app.example.com` and two backend replicas, configure these public Domains:

| Domain                  | Service                                         | Allowed origins                |
| ----------------------- | ----------------------------------------------- | ------------------------------ |
| `app.example.com`       | Leave blank unless the frontend needs discovery | Leave blank for its own origin |
| `app-api-1.example.com` | `app-api`                                       | `https://app.example.com`      |
| `app-api-2.example.com` | `app-api`                                       | `https://app.example.com`      |

Link each backend Domain to its container and application port. The frontend queries
`https://discovery.example.com/?service=app-api`, then calls one of those backend names over HTTPS.
An optional **pool** groups members within a service, for example by region.

Allowed origins are set on each backend Domain. A blank field follows that Domain's own current HTTPS origin;
it does not infer the frontend. Explicit origins are plain values, so renaming the frontend requires updating
each backend that names it. See [Allowed Origins](architecture.md#allowed-origins) for the application CORS policy.
Discovery and HTTP latency probes are independently readable from any origin without cookies.

## Discovery contract

Use HEAD or GET with a URL-encoded `service` query parameter:

| Discovery URL                                       | Members                       | Connection                                       |
| --------------------------------------------------- | ----------------------------- | ------------------------------------------------ |
| `https://discovery.<domain>/?service=<service>`     | Public HTTP Domains           | HTTPS, port 443                                  |
| `https://discovery.int.<domain>/?service=<service>` | Internal HTTP and TCP Domains | Tailnet only; HTTPS 443 or TCP with TLS/SNI 8443 |

Read the **`Sage-Endpoints` response header**, not the response body. For example:

```http
Sage-Endpoints: "app-api-1.example.com";pool="eu";online, "app-api-2.example.com";pool="us";online=?0
Cache-Control: private, max-age=30
```

This is an [RFC 9651 Structured Fields list](https://www.rfc-editor.org/rfc/rfc9651.html#section-3.1.2).
Use a Structured Fields parser rather than splitting on commas or semicolons. Each member is a hostname string
with a Boolean `online` parameter and an optional string `pool`. Bare `;online` means true; `;online=?0` means
false. There are no priority, weight, protocol or port parameters. Ignore unknown parameters.

All service-assigned Domains with a resolvable name remain listed, including unlinked records and records with
pending or failed sync. `online` requires an active container, a worker meeting Sage's cached readiness policy,
and a successful Domain sync matching its current configuration. This is last-known state: detection, publication
and client caching introduce lag. It is neither a live health check nor a promise that an application request succeeds.

A service with only offline members still returns its list. An unknown service or unsupported request method
returns 404. Discovery allows browser access to the header; its response cache lifetime is 30 seconds, separately
from the 300-second CORS preflight lifetime.

## Selection and failover

Keep membership and the selected connection as separate client state, scoped to the discovery URL, service and
pool. A practical policy is:

1. Fetch and parse discovery, honoring its HTTP cache policy. A valid response replaces the complete inventory,
   including its offline members. Select only `online` members matching the requested pool. A valid response with
   no eligible members means unavailable; do not fall back to online flags from an older response.
2. On a temporary discovery failure, retain the most recent valid inventory with its original age. Use it only
   within an explicit stale budget appropriate to the application, for example five minutes since the last
   valid network refresh. A network error, timeout, 5xx or malformed header must not overwrite that inventory.
   Treat 404 as unavailable rather than a successful empty list or a reason to keep retrying an obsolete service.
3. Shuffle eligible candidates before probing. For HTTP, time credentialless HEAD requests to `/ping`, with a
   timeout and a successful status required. One warm-up followed by a few measurements reduces connection-setup
   noise. A missing route, timeout, CORS failure or unsuccessful status excludes that candidate for this pass.
   This measures the network and Traefik path, not application health. GET `/ping` belongs to the application.
4. Choose uniformly at random among candidates within a small window of the fastest, for example 20 ms. Taking
   the strict minimum for every client concentrates traffic on one replica. Treat the window and timeout as
   application choices rather than Sage configuration.
5. Retain a working selection between requests. Refresh membership when needed under the cache policy; probe
   when choosing or reconsidering a target, rather than on every application request. Switch a working target
   only for a clear sustained gain, for example more than 20 ms across consecutive measurement passes. Remove it
   immediately from selection if a valid discovery update removes it or marks it offline.
6. On a transport failure, temporarily penalize the selected replica and choose another eligible candidate.
   For example, exclude the failed host for 30 seconds and try a bounded number of alternatives within the
   caller's overall deadline. Track penalties separately from membership; do not change Sage's `online` flag.
   Classify application errors before retrying; for example, a validation error is not a transport failure.

The HTTP cache's freshness and your re-probe policy are different clocks. Browsers manage the discovery response
cache for ordinary fetches. Other clients should use an HTTP cache or implement its freshness rules; reading a
cached response must not reset the stale budget as if it were a new network refresh. See
[HTTP caching](https://www.rfc-editor.org/rfc/rfc9111.html#section-4.2).

Failover does not make replay safe. A timed-out write may already have committed on the first replica. Retry only
requests whose semantics permit it, or use application-supported idempotency keys and shared deduplication across
replicas. Do not blindly replay POST or other non-idempotent operations. See
[HTTP retry semantics](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2).

## Browser example: initial HTTP selection

This small example performs one discovery/selection pass for a modest number of HTTP replicas. It uses GET for
discovery and reads its body to completion, leaving response caching to the browser. It throws when
discovery or every eligible probe fails. The application owns the last-good inventory, stale budget, retained
selection and retry policy described above; this function does not implement those stateful policies.

In your frontend project, install [`structured-headers`](https://github.com/evert/structured-headers):

```bash
npm install structured-headers@2.1.1
```

Save this as `replica.js` and import it through your frontend bundler:

```js
import { parseList } from 'structured-headers'

export async function chooseReplica(discoveryUrl, pool) {
  const response = await fetch(discoveryUrl, {
    method: 'GET',
    mode: 'cors',
    credentials: 'omit',
    cache: 'default',
    redirect: 'error',
    signal: AbortSignal.timeout(2000),
  })
  if (!response.ok) throw new Error(`Discovery returned ${response.status}`)
  await response.text()
  const header = response.headers.get('Sage-Endpoints')
  if (header === null) throw new Error('Missing Sage-Endpoints header')

  const members = parseList(header).map(([host, params]) => {
    if (
      typeof host !== 'string' ||
      typeof params.get('online') !== 'boolean' ||
      (params.has('pool') && typeof params.get('pool') !== 'string')
    ) {
      throw new Error('Invalid discovery member')
    }
    const url = new URL(`https://${host}`)
    if (url.hostname !== host.toLowerCase() || url.origin !== `https://${host.toLowerCase()}`) {
      throw new Error('Invalid discovery host')
    }
    return { origin: url.origin, online: params.get('online'), pool: params.get('pool') }
  })
  const candidates = members.filter(
    member => member.online && (pool === undefined || member.pool === pool)
  )
  if (!candidates.length) throw new Error('No eligible replicas')
  for (let i = candidates.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1))
    ;[candidates[i], candidates[j]] = [candidates[j], candidates[i]]
  }

  const measured = await Promise.all(
    candidates.map(async ({ origin }) => {
      let ms = Infinity
      try {
        for (let sample = 0; sample < 4; sample++) {
          const started = performance.now()
          const probe = await fetch(`${origin}/ping`, {
            method: 'HEAD',
            mode: 'cors',
            credentials: 'omit',
            cache: 'no-store',
            redirect: 'error',
            signal: AbortSignal.timeout(2000),
          })
          if (!probe.ok) throw new Error(`Probe returned ${probe.status}`)
          if (sample > 0) ms = Math.min(ms, performance.now() - started)
        }
        return { origin, ms }
      } catch {
        return null
      }
    })
  )
  const reachable = measured.filter(member => member !== null)
  if (!reachable.length) throw new Error('No reachable replicas')
  const fastest = Math.min(...reachable.map(member => member.ms))
  const nearby = reachable.filter(member => member.ms <= fastest + 20)
  return nearby[Math.floor(Math.random() * nearby.length)].origin
}
```

Call it from the frontend, replacing the API path with your application's own. To restrict selection to a pool,
pass its name as the second argument to `chooseReplica`, for example `'eu'`:

```js
import { chooseReplica } from './replica.js'

const discovery = new URL('https://discovery.example.com/')
discovery.searchParams.set('service', 'app-api')
const origin = await chooseReplica(discovery)
const response = await fetch(new URL('/v1/items', origin), { credentials: 'omit' })
if (!response.ok) throw new Error(`API returned ${response.status}`)
const items = await response.json()
```

Each candidate gets one warm-up and three timed probes, in parallel across candidates. Measurements include
browser scheduling and HTTP overhead; they are latency estimates, not exact RTTs. A pass can take up to roughly
ten seconds with these timeouts. For a large service, bound the number of concurrent probes and the overall
selection deadline. A chosen target may still fail an API request despite a successful probe.

Use normal CORS fetches: `no-cors` gives an opaque response whose success status cannot be checked. This example
omits cookies; the origins editor is not a credentialed cross-origin cookie policy. Application custom response
headers also need their own exposure policy if browser code must read them. See the
[Fetch CORS protocol](https://fetch.spec.whatwg.org/#http-cors-protocol).

## Internal and TCP clients

Internal HTTP uses the same algorithm with `discovery.int.<domain>` and the returned HTTPS names. The client
must be on the tailnet. HTTP and TCP share this discovery namespace, so configure a service consistently with
the client's protocol; Sage does not enforce homogeneous service protocols or advertise a per-member type.

For TCP, connect to the returned hostname on **8443**, with TLS and that hostname as SNI. Probe with the actual
protocol's connection/readiness operation and a timeout; HTTP HEAD `/ping` is not a TCP probe. Follow the client
library's connection-pool and retry semantics, including transaction safety. See
[Domain Types](architecture.md#domain-types) for TLS/SNI examples.

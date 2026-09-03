# ADR-0004: 단일 호스트 Docker Compose와 DockerSpawner

- 상태: Accepted (2026-08-10)
- 날짜: 2026-08-06
- 결정 범위: MVP 배포·사용자 실행 격리·영속 데이터

> 2026-08-11 이후 host `DOCKER-USER` 운영과 private/shared hard quota 결정은
> [ADR-0006](0006-trusted-network-shared-storage.md)이 대체한다. 나머지 단일 호스트,
> DockerSpawner, volume ownership 및 control-plane 결정은 계속 유효하다.
> 따라서 아래 Docker 28 isolated gateway, host firewall health와 `DOCKER-USER` 규칙은
> `legacy-host-firewall-v1`의 역사적 계약에만 적용된다. 현재 production Compose의
> Engine 28+ base isolated-gateway / Engine 27 `inhibit_ipv4` compatibility overlay 계약에는
> ADR-0006을 적용한다.
> 2026-08-11 이후 exact workspace volume의 자동 wipe/recreate 절차는
> [ADR-0007](0007-admin-runtime-and-environment-management.md)이 아래 수동 삭제 결정을
> 대체한다.
> `cyberailabs.team`의 DNS/TLS/origin과 같은 registrable domain 위험 승인은
> [ADR-0009](0009-cyberailabs-production-domain.md)이 이 문서의 해당 출시 gate를 구체화한다.

## 맥락

등록 사용자는 팀원 10명, 사용자당 환경은 최대 5개, 전체 active server는 최대 15개이고 Docker Compose 배포가 확정됐다. 팀원은 상호 신뢰하지만 private code/data는 격리하고 별도 shared directory로만 공유한다. 사용자 환경은 package 다운로드용 외부 통신 외에는 사내 시스템에 접근하지 않는다. 사용자 notebook은 임의 코드를 실행하므로 런타임은 비신뢰로 취급한다.

## 결정

전용 Linux host 한 대에서 정적 control-plane service는 Docker Compose로, 사용자 JupyterLab container는 DockerSpawner로 실행한다.

정적 Compose service:

- `gateway`: 유일한 공개 port `80/443`, TLS·host routing·WebSocket·login rate limit
- `frontend`: React 정적 파일
- `api`: FastAPI와 Hub OAuth callback, MVP process/replica 1
- `migrate`: platform migration one-shot job
- `worker`: lifecycle operation 처리, replica 1
- `reconciler`: read-only Hub snapshot과 실제 상태/freshness 감사, replica 1
- `jupyterhub`: NativeAuthenticator, Hub, configurable proxy, DockerSpawner
- `egress-proxy`: 승인된 외부 package/Git repository만 중계

동적 `jupyter-<user>-<server>` container는 `compose.yaml`에 열거하지 않는다. 최대 50개 workspace record/volume 중 최대 15개가 active일 수 있다. DockerSpawner가 host daemon을 통해 생성·중지하며 pre-provisioned `jupyter` external network, DB가 배정한 workspace private-volume slot과 팀 shared volume에 연결한다.

네트워크는 다음처럼 나눈다.

- `edge`: gateway, frontend, api, Hub public proxy
- `control`: api, worker, reconciler, Hub 내부 API
- `jupyter`: `internal=true`, `com.docker.network.bridge.enable_icc=false`, `com.docker.network.bridge.gateway_mode_ipv4=isolated`, IPv6 off인 Hub proxy/egress-proxy/동적 single-user 내부망
- `egress-out`: dual-home egress-proxy만 연결하는 외부망

사용자 container는 `control`/`egress-out`에 연결하지 않고 어떤 내부 service도 host port로 publish하지 않는다. root-owned host provisioning이 `jupyter` external network의 고정 subnet, 동적 `ip_range`, 그 범위 밖 Hub/egress-proxy `aux_addresses`를 먼저 만든다. `internal=true`, isolated gateway, IPv6 off와 Docker-owned ICC baseline deny를 fail-closed 바닥선으로 두어 bridge device에 host gateway 주소를 만들지 않으며 `host.docker.internal`/`host-gateway`도 추가하지 않는다. host firewall은 그 위에 고정 endpoint exact-port만 열어 `Hub/proxy → user`, `user → 필수 Hub endpoint`, `user → egress-proxy`만 허용한다. 이 option과 internal-network DNS exfiltration 수정이 포함된 Docker Engine 28+ exact version/network backend를 고정해 allow rule 조합을 검증한다. user container의 외부 DNS query가 attacker-controlled authoritative canary에 도달해서도 안 된다.

검증 결과는 `/run/platform-health`의 atomic `network_health` manifest로 Hub/API/worker에 read-only 제공하며 false·누락·stale이면 create/start/spawn을 거부한다. root health는 bridge의 IPv4 gateway 및 routable IPv6 주소 부재와 host service bind inventory도 확인한다. Docker 28/Linux가 host bridge에 자동 생성하는 `fe80::/10` link-scope 주소는 network IPv6가 비활성이고 endpoint에 IPv6가 할당되지 않은 경우에만 허용한다. boot·Docker restart 전에 기존 health를 invalid 처리한 뒤 firewall을 재적용한다. continuous host watchdog이 platform allow-rule drift를 찾으면 emergency DROP과 egress-proxy 격리를 수행하고, Docker-owned ICC/isolated-gateway baseline drift면 exact user-container inventory도 cgroup-freeze/`docker pause` 후 controlled stop한다. 자동 resume은 금지한다. host gateway/SSH/관리 port, user-to-user port, direct egress, RFC1918/link-local/metadata/control subnet을 차단한다. egress-proxy는 승인된 package/Git repository만 허용하고 DNS 결과 IP도 재검사한다.

영속 데이터는 다음처럼 분리한다.

- `platform_data`: platform SQLite
- `hub_data`: Hub SQLite와 NativeAuthenticator bcrypt hash
- `jupyter-user-<username>-slot-<1..5>`: 승인 사용자의 첫 portal login으로 platform user가 생긴 뒤 quota를 부여하는 workspace private slot
- `jupyter-shared`: 전 팀원이 명시적으로 공유한 자료
- 보호된 secret file: Hub cookie secret, OAuth client secret, proxy auth token, portal token 암호화 key, spawn-validator HMAC key

SQLite DB는 로컬 disk에 두고 platform/Hub DB를 서로 다른 파일·migration·backup 대상으로 관리한다. 사용자의 `stop`은 container compute만 제거하고 volume은 보존한다. 영구 삭제는 self-service가 아니다. 관리자가 먼저 SQLite deletion tombstone을 원자적으로 설정해 start/launch/ticket을 차단한 뒤 stop → Hub record remove → unmounted 확인 → exact slot wipe/re-provision → archive checkpoint를 수동 수행하고 감사 기록을 남긴다.

shared volume은 host bootstrap 때 만든다. 승인 사용자의 첫 portal login은 신규 platform user만 `PROVISIONING`으로 만들고 기존 `DISABLED`를 재활성화하지 않는다. 그 뒤 root-owned one-shot volume provisioner가 exact user UUID/username에 private slot 5개와 nonzero·globally unique project ID/hard quota, `PROJINHERIT`, 고정 UID/GID, 검증 label을 설정한다. private/shared directory는 Docker data-root와 분리된 전용 XFS quota filesystem에 두어 overlay writable-layer project-ID namespace와 충돌하지 않게 한다. manifest·실제 quota를 DB inventory와 대조한 뒤 conditional `PROVISIONING → ACTIVE`만 허용하며 그 전 workspace create는 `PROVISIONING_REQUIRED`로 거부한다. MVP의 모든 enabled profile은 같은 private disk hard limit을 사용한다. API/worker에는 이 권한이나 Docker socket을 주지 않는다. composite owner FK와 ticket/apply/pre-spawn owner join으로 workspace와 slot owner가 다르면 실패 시 닫는다. `BEGIN IMMEDIATE`로 profile limit과 일치하는 검증된 free slot을 workspace에 배정하며 Spawner는 DB inventory와 host/Docker label이 맞지 않으면 mount하지 않는다.

root-owned host health unit은 boot와 Docker restart 뒤 storage driver, XFS `pquota`/`prjquota` mount option, 각 slot tree의 nonzero·unique project ID, project-tree 일관성, `PROJINHERIT`, hard limit과 shared quota를 조회한다. 결과, host boot ID, Docker generation과 짧은 expiry를 재부팅 때 비워지는 root-owned `/run/platform-health`의 atomic `storage_health` manifest로 발행해 Hub/API/worker에 read-only 제공한다. Docker restart 전에는 먼저 invalid 처리한다. API preflight와 `apply_user_options/pre_spawn_hook`은 현재 boot/generation에 대해 healthy이면서 fresh하지 않으면 모든 spawn을 실패 시 닫는다. running session에서 watchdog이 quota/mount/inheritance drift를 찾으면 먼저 unhealthy로 바꾸고 exact user-container inventory를 즉시 cgroup-freeze/`docker pause`한 뒤 controlled stop한다. 자동 resume은 금지하고 storage 재검증과 관리자 승인 뒤 정상 start만 허용한다. DB/label만으로 quota 활성 상태를 추정하지 않는다.

## Docker socket에 대한 명시적 위험 수용

DockerSpawner가 container 안에서 host daemon을 사용하려면 JupyterHub가 Docker API에 접근해야 한다. raw Docker socket은 사실상 host root급 권한이다.

- long-running service 중 socket은 `jupyterhub` container에만 mount한다. host one-shot provisioner는 제한된 관리 실행 중에만 필요한 권한을 사용한다.
- 명시적 `local-dev` 또는 ADR-0009의 exact production Docker-volume mode에서는 웹
  self-service volume 준비 agent를 별도 socket 보유
  container로 만들지 않고 JupyterHub managed service로 실행한다. API/worker에는 socket을
  추가하지 않으며, production에서는 별도 capability flag와 unlimited named-volume 정책이
  동시에 없으면 설정 검증에 실패한다.
- api, worker, frontend와 사용자 container에는 socket을 절대 전달하지 않는다.
- 사용자 입력으로 image, volume, host path, network, privileged/capability 옵션을 만들지 않는다.
- 이 host에는 다른 부서의 민감 workload와 장기 credential을 함께 두지 않는다.
- socket proxy는 공격 표면을 줄일 수 있으나 container 생성 권한 자체가 강력하므로 근본 격리로 간주하지 않는다.

Hub가 침해되면 같은 daemon의 control-plane container와 secret도 영향권에 들어갈 수 있다. 이것이 이 결정의 가장 큰 잔여 위험이다.

## 사용자 container 기준

- non-root, privileged/host PID/IPC/network/host path 금지
- Docker socket과 다른 사용자 volume 금지
- `cap_drop=ALL`로 `NET_RAW`/`NET_ADMIN` 포함 capability 제거, `no-new-privileges`, seccomp/AppArmor 적용
- 승인된 single-user image를 digest 또는 불변 tag로 고정
- `disable_user_config=True`, absolute command, read-only single-user server environment; package는 private kernel/venv에 설치
- CPU, memory, pids, server 수 상한 필수
- Hub `named_server_limit_per_user=5`, `active_server_limit=15`를 최종 실행 한도로 설정
- admin을 포함한 모든 사용자의 default server와 platform one-time ticket 없는 named server spawn을 Hub hook에서 거부
- DB가 배정하고 quota 검증된 workspace private slot만 해당 container에 mount
- 고정 UID/GID와 공용 group/setgid로 `jupyter-shared`를 전원 read-write mount
- user-to-user network, metadata, 사내망과 control-plane 접근 차단
- egress-proxy도 non-root, `cap_drop=ALL`, `no-new-privileges`, read-only로 실행하고 source spoof/L3 deny 우회 시험
- workspace별 private hard quota, shared 총 hard quota, 검증된 storage driver의 writable layer/tmpfs/shm limit
- 정적·동적 container Docker log driver에 `max-size`/`max-file` 또는 동등한 hard limit
- control-plane data/log를 별도 quota/filesystem에 두고 예약 공간 확보

전체 active 15개는 확정됐지만 정확한 환경별 CPU/RAM/disk 값과 host 사양은 workload가 미확정이므로 이 ADR에서 고정하지 않는다. 부하 시험으로 15개와 control-plane reserve를 동시에 감당할 quota/host를 정하기 전에는 팀 공개하지 않는다.

shared volume은 모든 팀원이 읽고 수정·삭제할 수 있는 공동 신뢰 영역이며 secret/개인 데이터와 PATH/PYTHONPATH/Jupyter config/startup 파일을 두지 않는다. private 파일은 사용자가 명시적으로 shared로 복사해야만 공유된다. 중요 공유 자료는 version control 또는 backup으로 보호한다.

브라우저 수준의 private data 격리를 위해 Hub control host와 사용자 server를 per-user subdomain으로 분리하고 wildcard DNS/TLS와 host-prefix cookie를 사용한다. portal은 user-content와 다른 registrable domain에 두고 모든 민감 portal cookie도 `__Host-`로 만든다. 이 DNS/TLS/cookie 경계를 제공할 수 없으면 공식 문서상 사용자 간 보호 보장이 없으므로 별도 위험 승인 전에는 팀 데이터를 공개하지 않는다.

## 대안 비교

### KubeSpawner/Kubernetes

network policy, resource quota, 여러 node, 다중 GPU·MIG·공유 GPU scheduling과 HA에 더 적합하다. 조직에 기존 Kubernetes가 있어 전환 비용도 일반적인 신규 cluster보다 낮다. 그럼에도 사용자가 Compose 우선으로 확정했으므로 MVP에는 채택하지 않는다. 현재 Compose 구현은 운영자가 허용한 물리 NVIDIA GPU 한 개의 workspace별 독점 할당만 지원한다. Docker에서 user-to-user/egress 격리를 신뢰성 있게 강제하지 못하거나 15개가 단일 host 용량을 넘으면 우선 전환 대안이다.

### LocalProcessSpawner

Docker socket이 필요 없고 구성이 작다. 반면 사용자 process와 파일 권한·resource 격리가 host OS 계정에 의존하며 이번 NativeAuthenticator/Docker 환경과 맞지 않는다. 채택하지 않는다.

### 사용자별 container를 Compose service로 고정

사용자별 최대 5개, 잠재적으로 50개 service를 미리 만들면 사용자 승인, named server, start/stop과 상태 원천이 Compose와 Hub로 분리된다. JupyterHub가 실제 수명주기를 소유한다는 결정에 어긋나므로 채택하지 않는다.

### JupyterHub만 host에서 직접 실행

socket mount 없이 host daemon을 사용할 수 있지만 Hub dependency와 Python 환경을 host에 설치하고 Compose의 배포·복구 경계를 깨뜨린다. socket 권한의 본질적 위험도 사라지지 않아 채택하지 않는다.

## 결과와 비용

장점:

- 10명 규모에서 배포·복구·관찰 구조가 이해하기 쉽다.
- 사용자 server는 필요할 때만 생기고 workspace별 private volume과 팀 shared volume이 유지된다.
- platform DB, Hub credential DB, 사용자 데이터를 별도로 복구할 수 있다.

비용과 잔여 위험:

- host 한 대의 장애와 용량이 전체 서비스 장애·상한이다.
- Docker socket으로 Hub 침해 blast radius가 host 전체로 커진다.
- Docker bridge는 Kubernetes 수준의 network policy를 제공하지 않는다.
- 동적 user-to-user 차단과 package-only egress firewall rule 운영이 복잡하다.
- volume hard quota와 backup을 별도로 구현·운영해야 한다.

## 출시 gate

- gateway 이외 port가 host에 publish되지 않는다.
- 사용자 container에서 control network, Docker socket, metadata, 사내망, 다른 사용자 private volume/port에 접근할 수 없다.
- host boot와 Docker restart 뒤 firewall health 전에는 spawn이 실패한다. 실행 중 session에서 custom forwarding rule을 제거해도 `internal`/isolated-gateway/IPv6-off/ICC baseline으로 host gateway·SSH·관리 port, direct egress/east-west가 열리지 않고, `cap_drop=ALL` container의 ARP/IP spoof와 허용 port 밖 통신이 실패한다.
- user container의 외부 DNS query가 실패하고 attacker-controlled authoritative DNS canary에는 질의 흔적이 없다.
- package repository는 egress-proxy로 접근되지만 direct egress와 allowlist 밖 목적지는 실패한다.
- egress-proxy 자체에서도 IPv4/IPv6 private·사내 public CIDR·metadata 목적지 연결이 실패한다.
- 사용자당 6번째 workspace는 platform transaction/Hub record limit에서, 전체 16번째 active server는 Hub에서 거부된다.
- private volume은 상호 격리되고 `jupyter-shared`만 공동 read-write이며 backup/restore가 동작한다.
- 첫 portal login→`PROVISIONING`→exact user slot provision/manifest import→`ACTIVE` 순서가 유지되고 준비 전 workspace create가 실패한다.
- quota provisioner manifest, DB slot inventory, Docker volume label, nonzero·unique project ID/tree, `PROJINHERIT`와 실제 hard limit이 일치하며 cross-owner·미검증 volume mount가 실패한다.
- quota option을 끈 reboot와 false·누락·stale `storage_health` 상태에서는 모든 spawn이 실패하고, 실행 중 quota/inheritance drift는 모든 user container를 검증된 시간 안에 freeze/stop한다.
- 새 파일이 slot project ID를 상속하고 hard limit까지 실제 쓰기가 거부되며 private/shared와 Docker writable-layer project ID namespace가 충돌하지 않는다.
- CPU/RAM/PID/private/shared/writable-layer/tmpfs/stdout-log 고갈 시험에서 Hub와 다른 사용자가 생존한다.
- WebSocket, 큰 파일 upload, idle timeout이 gateway 경유로 동작한다.
- 서로 다른 registrable domain의 portal/user-content와 per-user wildcard domain에서 사용자 A/B origin과 cookie가 분리되고 cookie tossing이 실패한다.
- Compose/Hub/API/worker 재시작 후 동적 container와 workspace 상태가 다시 수렴한다.
- platform DB, Hub DB, user volume, secret의 backup/restore를 실제로 시험한다.
- start/worker 재시도와 경합하는 수동 삭제 runbook이 tombstone/checkpoint로 정확한 한 workspace slot만 wipe/re-provision하고 platform/Hub 상태와 감사를 갱신한다.
- image와 dependency digest, SQLite runtime 버전을 기록한다.

## 재검토 조건

- 최대 동시 실행량이 단일 host 용량을 초과함
- 비신뢰 사용자 간 강한 격리 또는 민감 내부망 접근이 필요함
- 단일 물리 GPU 독점 범위를 넘는 다중 GPU·MIG·time-slicing, 여러 node, HA,
  세밀한 network policy/resource quota가 필요함
- Docker에서 user-to-user 차단 또는 package-only egress를 안정적으로 강제할 수 없음
- Docker socket의 host root급 위험을 수용할 수 없음

## 근거 자료

- [DockerSpawner types](https://jupyterhub-dockerspawner.readthedocs.io/en/stable/spawner-types.html)
- [DockerSpawner data persistence](https://jupyterhub-dockerspawner.readthedocs.io/en/stable/data-persistence.html)
- [DockerSpawner image selection](https://jupyterhub-dockerspawner.readthedocs.io/en/latest/docker-image.html)
- [Docker bridge network options](https://docs.docker.com/engine/network/drivers/bridge/)
- [Docker Engine 25 security fixes](https://docs.docker.com/engine/release-notes/25.0/)
- [JupyterHub Security Overview](https://jupyterhub.readthedocs.io/en/stable/explanation/websecurity.html)
- [Appropriate Uses For SQLite](https://www.sqlite.org/whentouse.html)

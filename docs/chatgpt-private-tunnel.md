# 웹·휴대폰용 비공개 터널 연결 — 도메인·Auth0 없이

현재 선택: 기존 Ubuntu 서버, ChatGPT Plus, 추가 유료 서비스 가입이나 도메인 구매 없음.
공식 Secure MCP Tunnel의 **비공개 stdio 연결**을 우선 검증합니다.
터널 설정 화면에 결제 안내가 없다는 사실만으로 터널 요금이 0원이라고 확정하지 않습니다.
공식 연결 문서에는 요금 보장이 명시되어 있지 않습니다. 결제·충전·유료 전환을
요구하면 진행하지 않고, 실제 실행 전 계정의 비용 조건을 확인해야 합니다.
이 프로젝트는 Responses API 등 유료 모델 API를 호출하도록 바꾸지 않습니다.

## 지금 사용자님이 할 일

1. [OpenAI 터널 설정](https://platform.openai.com/settings/organization/tunnels)에서
   본인 개인 조직을 선택하고 이 프로젝트용 터널을 하나 생성합니다.
2. 사용할 **본인 ChatGPT 워크스페이스**를 연결합니다. 다른 조직·팀과 공유하지 않습니다.
   조직 연결만으로 해당 워크스페이스에 자동 표시되는 것은 아닙니다.
3. 생성된 `tunnel_id`만 알려주세요. API 키, 토큰, 계정 비밀번호는 채팅에 보내지 마세요.
4. ChatGPT 웹에서 개발자 모드를 켤 수 있는지, MCP 연결 추가 화면의 Connection에
   **Tunnel**이 제공되는지도 확인합니다. 터널 설정에 접근하는 권한과 ChatGPT
   개발자 모드 권한은 별개입니다.

runtime API key도 필요하지만 **키 생성은 과금 조건을 확인한 다음** 진행합니다.
필요한 터널 권한만 가진 키를 Ubuntu 서버의 owner-only 파일에 직접 저장하도록
별도 안내합니다. 키를 명령행 인수나 셸 히스토리에 넣거나 채팅에 붙이지 않습니다.

## 서버 구성

```text
ChatGPT 웹 / 휴대폰 (해당 연결을 사용할 수 있는 계정)
    → 계정·워크스페이스에 연결된 OpenAI 터널
    → Ubuntu의 tunnel-client (outbound HTTPS)
    → remote-mcp-tunnel-stdio (자식 프로세스, 공개 HTTP 포트 없음)
    → Gateway (127.0.0.1:8765)
    ← 같은 Ubuntu의 Agent (loopback WebSocket)
```

여기서 stdio는 **서버 안의 연결 방식**입니다. 사용자가 데스크톱/Codex로 옮겨야 한다는
뜻이 아닙니다. ChatGPT 쪽은 원격 터널 연결을 선택합니다.
처음 검증할 Agent는 같은 Ubuntu 서버에 둡니다. 다른 네트워크의 기기는 Gateway까지의
별도 안전한 네트워크 경로가 필요하며, MCP 터널이 Agent WebSocket까지 대신 연결하지 않습니다.

public Caddy/MCP HTTP 서비스, Auth0, 공개 MCP bearer token은 이 경로에 사용하지 않습니다.
Gateway 인증·Agent enrollment·파일 허용 경로·외부 승인 절차는 그대로 유지합니다.
단, 본인 접근 여부는 MCP의 Auth0 사용자 검사 대신 **터널의 조직·워크스페이스 권한**에
의존하므로 실제 공유 범위와 다른 계정의 접근 거부를 검증해야 합니다.
다른 사용자가 있는 공유 조직에서는 곧바로 '본인 전용'이라고 간주하지 않습니다.

## 실행 준비 — 아직 실제 설치·실행한 명령이 아님

- 공식 터널 설정 화면의 다운로드 또는 공식 문서의 최신 release 링크로
  `tunnel-client`를 설치하고 제공되는 무결성 검증 방법을 확인합니다.
- `tunnel-client help quickstart`로 설치한 버전의 옵션을 먼저 확인합니다.
- Gateway와 동일한 호스트에 설치한 Commander의 `remote-mcp-tunnel-stdio` 절대경로를
  사용합니다. root로 실행하지 않습니다.
- 실제 비밀 환경 파일은 `deploy/tunnel.env.example`을 기준으로 준비하고 owner-only로
  보관합니다. 이 파일을 git에 추가하지 않습니다.
- 런타임 API 키는 터널 클라이언트에만 필요합니다. 전용 stdio 실행기는 그 키와
  approval-admin/Agent 비밀을 MCP 자식의 환경에서 제거하며 작업 폴더의 `.env`를 읽지 않습니다.

공식 stdio 프로필 절차의 형태는 다음과 같습니다. 먼저 해당 환경을 **안전하게 로드한
상태**여야 합니다. 예시 경로를 실제 설치 경로로 바꿔야 하며 키 원문은 명령에 넣지 않습니다.

```bash
tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile remote-commander \
  --tunnel-id "$COMMANDER_TUNNEL_ID" \
  --mcp-command "/opt/remote-mcp-commander/current/.venv/bin/remote-mcp-tunnel-stdio"

tunnel-client doctor --profile remote-commander --explain
tunnel-client run --profile remote-commander
```

이 설정은 프로필을 생성하고 네트워크 요청을 할 수 있습니다. 비용 조건·권한을
확인하기 전에는 실행하지 않습니다. 설치 버전의 프로필 저장 경로·서비스 실행 계정과
환경 전달 동작을 확인한 다음에 상시 실행용 systemd 구성을 확정합니다.
터널 클라이언트를 켜둬야 ChatGPT가 도구를 발견하고 호출할 수 있습니다.

설치한 공식 클라이언트의 도움말에서 장기 로컬 실행을 에이전트가 관리할 때는
`tunnel-client runtimes connect`를 권장합니다. 기존 터널에 `--tunnel-id`로 연결하고,
`--runtime-api-key file:/<owner-only-key-file>`처럼 키 **참조**만 전달합니다.
키 원문은 인수에 넣지 않습니다. 실행 후 `runtimes status <alias> --json`의
`process_running`, `healthy`, `ready`를 각각 확인하기 전에는 실행 성공을 보고하지 않습니다.
실제 플래그 순서는 설치 버전의 `runtimes status --help`도 확인하세요.

## 연결·완료 기준

1. ChatGPT 웹의 개발자 모드 MCP 추가 화면에서 Connection → Tunnel을 선택합니다.
2. 본인의 터널을 선택하거나 `tunnel_id`를 입력합니다.
3. 도구 목록을 확인하고, 새 대화에서 `list_devices`, `ping_device`, `system_health`를
   먼저 실행합니다. Agent가 없으면 터널 연결 성공과 별개로 Agent를 등록해야 합니다.
4. 같은 계정의 휴대폰에서 해당 연결을 선택해 **새 요청을 실제 실행**합니다.
   PC에서 만든 답변을 휴대폰에서 볼 수 있는 것만으로는 성공으로 간주하지 않습니다.
   일반 플러그인의 모바일 지원 안내가 개인 개발자 연결의 동일한 제공을 보장하지는
   않으므로 휴대폰 브라우저와 필요한 경우 네이티브 앱에서 직접 검증합니다.
5. 터널 재연결·서버 재시작, 다른 계정의 접근 거부를 확인합니다.
6. 사용량·비용 화면을 확인합니다. 추가 비용 없음과 본인 전용 접근을 입증하지 못하면
   사용자 조건을 충족한 것으로 보고하지 않습니다.

설정 준비 / 서버 설치 / 터널 실행 / 웹 성공 / 휴대폰 성공 / 비용 확인은 서로 다른 단계입니다.
실제 완료 상태를 각각 기록해야 합니다. 공개 무인증 엔드포인트나 비밀 URL만으로
원격 제어를 보호하는 방식으로 우회하지 않습니다.

공식 근거: [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels),
[ChatGPT MCP 연결·테스트](https://developers.openai.com/plugins/deploy/connect-chatgpt),
[플러그인의 지원 화면](https://learn.chatgpt.com/docs/plugins).

# 개인용 ChatGPT 연결 (Auth0)

> 현재 사용자 선택은 Auth0·도메인 구매 없는 비공개 터널입니다.
> `chatgpt-private-tunnel.md`를 먼저 보세요. 아래 Auth0 절차는 공개 HTTPS를 선택할 때의
> 대안이며 지금 계정 생성·도메인 구매를 진행할 필요가 없습니다.

이 문서는 **현재 Ubuntu 서버 + 본인 한 명 + Auth0** 기준입니다.
코드가 준비된 것과 실제 ChatGPT 연결 성공은 다릅니다. 아래 마지막 검증을
완료하기 전에는 연결 완료로 간주하지 않습니다.

## 사용자님이 먼저 할 일

1. ChatGPT 설정에서 개발자 모드를 사용할 수 있는지 확인합니다.
   공식 안내는 Settings → Security and login → Developer mode입니다.
   계정·워크스페이스 정책에 따라 제공 여부가 다를 수 있습니다.
2. Auth0 계정을 만들고 이 서비스용 tenant를 준비합니다.
   관리자 대시보드 로그인과 MCP를 사용할 사용자 로그인은 별개입니다.
   tenant 안에 본인의 로그인 사용자를 만들고 MFA를 설정하세요.
   데이터베이스 로그인 연결을 쓴다면 공개 회원가입을 비활성화하세요.
3. 서버로 연결되는 고정 HTTPS 주소를 정합니다. 예: `https://mcp.example.com/mcp`.
   기존 도메인의 서브도메인으로도 충분합니다. 개인용 MCP 서버 연결을 위해
   공개 플러그인 심사·배포를 할 필요는 없습니다.
4. 제어할 Agent와 파일 접근을 허용할 **정확한 폴더**를 정합니다.
   서버 전체나 홈 전체를 허용하지 마세요. PTY는 첫 연결에서 켜지 않습니다.

공유할 정보: 선택한 MCP 도메인, Auth0 tenant 도메인, 개발자 모드 제공 여부,
제어할 기기와 허용 폴더. 비밀번호, OAuth client secret, access/refresh token,
Gateway control/approval-admin token은 채팅에 붙이지 마세요.

## Auth0 설정

OpenAI 공식 인증 안내에 링크된 Auth0 MCP authorization 가이드를 기준으로
tenant의 MCP 호환 OAuth 흐름을 구성합니다. 일반 OAuth 앱을 만드는 것만으로
ChatGPT 호환성이 자동으로 완성되는 것은 아닙니다.

- API Identifier / access-token audience를 **정확한 MCP URL**로 설정합니다.
  예: `https://mcp.example.com/mcp`. 끝의 `/mcp`와 슬래시 여부까지 같아야 합니다.
- API 서명 알고리즘은 **RS256**, 사용할 permission/scope는 `commander:use`입니다.
- Authorization Code + PKCE **S256**을 지원해야 합니다.
- ChatGPT가 보내는 `resource`를 처리해 위 API audience의 JWT access token을
  발급하도록 MCP 호환 설정을 완료합니다. opaque token, ID token, 다른 API의
  access token, M2M client-credentials token은 이 서버에서 사용할 수 없습니다.
- ChatGPT OAuth client 식별은 제공자가 지원하는 CIMD 또는 DCR을 사용하거나,
  ChatGPT 연결 화면이 허용하는 경우 사전 등록된 OAuth client를 사용합니다.
  화면에 client ID/secret 입력이 없다면 임의로 고정 토큰을 전달하지 말고
  Auth0의 MCP client 등록 방식을 구성하세요.
- Redirect URI는 **실제 ChatGPT MCP 관리 화면에 표시된 값**을 Auth0에 등록합니다.
  callback-ID별 URI 또는 안정적인 URI가 사용될 수 있으므로 예전 예제 값을
  무조건 복사하거나 wildcard callback을 허용하지 않습니다.
- 본인 로그인 사용자의 Auth0 **User ID**를 확인합니다. 예: `auth0|...`.
  이메일이나 OAuth client ID가 아닙니다. 소셜 로그인을 선택하면 해당 identity의
  User ID를 사용해야 합니다.
- access token은 짧은 만료 시간을 사용하세요. 발급된 JWT를 요청마다 Auth0에
  introspection하지 않으므로 사용자 비활성화가 기존 토큰을 즉시 무효화하지는
  않습니다. 긴급 차단은 MCP 서비스를 중지하거나 허용 User ID를 변경하고 재시작합니다.

이 구현은 Auth0의 issuer 기준 `/.well-known/jwks.json`에서 공개 서명키를 가져옵니다.
issuer는 OIDC discovery의 `issuer`를 그대로 복사합니다. Auth0는 통상 마지막 `/`도
포함하므로 임의로 빼지 마세요. client secret은 MCP 서버에 필요하지 않습니다.

## 서버에서 제가 이어서 할 부분

도메인과 tenant가 정해지면 실제 환경 파일·TLS·Agent를 구성하고 점검합니다.
현재 코드는 배포 템플릿이며, OS 서비스·DNS·Auth0 설정은 아직 적용하지 않았습니다.

`deploy/mcp-chatgpt.env.example`을 MCP 서비스의 환경 파일로 사용합니다.
기존 static 클라이언트용 `deploy/mcp.env.example`과 혼동하지 마세요.

```dotenv
COMMANDER_MCP_AUTH_MODE=oauth
COMMANDER_MCP_TOKEN=
COMMANDER_MCP_ISSUER_URL=https://YOUR_TENANT.auth0.com/
COMMANDER_MCP_RESOURCE_URL=https://mcp.example.com/mcp
COMMANDER_MCP_OAUTH_SUBJECT=auth0|YOUR_USER_ID
COMMANDER_MCP_SCOPE=commander:use
```

Gateway control credential은 기존 Gateway와 같은 값으로 서버 내부에서만 설정합니다.
별도 approval-admin credential은 MCP 환경·ChatGPT에 전달하지 않습니다.
OAuth 모드는 static MCP token으로 우회할 수 없으며, 지정한 **한 User ID만** 허용합니다.

Caddy 예시의 MCP 도메인을 실제 도메인으로 바꾸고 TLS를 준비합니다.
8765/8766은 loopback만 유지합니다. 서버에는 Auth0 JWKS로 나가는 HTTPS 접근이
필요합니다. SDK의 Host/Origin 검사는 유지하며 설정한 공개 MCP host만 추가 허용합니다.
네이티브 서비스·Agent 등록은 `native-deployment.md`를 따릅니다.

서버의 서비스 환경 변수가 안전하게 로드된 상태에서:

```bash
remote-mcp-doctor mcp
remote-mcp-connect-check --json
```

온라인 점검은 공개 HTTPS resource metadata, 익명 요청의 401 OAuth challenge,
Auth0 OIDC/PKCE discovery, RS256 JWKS 게시 여부를 확인합니다. 로그인하거나 토큰을
발급하지 않습니다. 전부 PASS여도 실제 사용자 로그인·토큰 audience·도구 호출은
별도 검증해야 하므로 `chatgpt.login_and_tools`는 의도적으로 WARN을 출력합니다.

## 사용자님이 ChatGPT에서 할 부분

1. 개발자 모드를 켜고 Plugins의 추가 버튼에서 MCP 서버 연결을 만듭니다.
   실제 화면·제공 여부는 계정에 따라 다를 수 있습니다.
2. 서버 URL에 `https://<정한 도메인>/mcp`를 입력합니다.
3. Auth0 로그인 창에서 **서버에 지정한 본인 사용자**로 로그인하고 승인합니다.
4. 발견된 도구 목록을 확인하고 새 대화에서 이 연결을 선택합니다.
5. 아래 읽기 전용 요청부터 실행합니다.

   - “연결된 기기 목록 보여줘.” (`list_devices`)
   - “server-01 연결 상태 확인해줘.” (`ping_device`)
   - “server-01 CPU·메모리·디스크 상태 보여줘.” (`system_health`)
   - “server-01에 허용된 파일 폴더 보여줘.” (`list_file_roots`)

기기 목록이 비어 있으면 인증 성공과 별개로 Agent 등록·연결을 점검해야 합니다.
이후 허용 폴더 내 테스트 파일만 읽고 쓰며, 중요한 파일 변경·서비스 재시작·PTY는
나중에 확인합니다. 별도 승인 절차는 그대로 남아 있고 ChatGPT가 스스로 발급하지 못합니다.

## 완료 기준 / 오류 구분

- OAuth discovery 점검 성공 + 본인 Auth0 로그인 성공.
- 실제 Agent가 나타나고 `ping_device`와 상태 조회 성공.
- 로그아웃·재연결 및 토큰 만료 후 재인증 동작 확인.
- 다른 User ID, 잘못된 audience/scope/서명, 만료된 토큰, static token은 거부.
- 허용하지 않은 파일 경로는 거부. 비밀 값이 응답이나 점검 출력에 나타나지 않음.

401: issuer/audience/User ID/scope/만료/키를 확인합니다. 토큰 원문을 공유하지 마세요.
`invalid_client`/callback 오류: Auth0 client 등록과 실제 redirect URI를 확인합니다.
421: Caddy domain·MCP resource URL·요청 Host가 같은지 확인합니다.
인증 성공 후 기기 없음: Gateway/Agent enrollment·서비스 상태를 확인합니다.

## 공식 근거

- [OpenAI OAuth/MCP 인증 안내](https://developers.openai.com/plugins/build/auth)
  (Auth0 MCP 설정 가이드 링크 포함)
- [ChatGPT MCP 연결·테스트 안내](https://developers.openai.com/plugins/deploy/connect-chatgpt)
- [공개 노출 없이 연결하는 Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
  지원되는 계정에서 가능한 대안이며 이 문서의 Auth0/HTTPS 배포와 별개로 설정해야 합니다.

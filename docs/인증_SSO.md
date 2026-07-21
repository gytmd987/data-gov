# 로그인(SSO) 설정 가이드

관리 화면(문서 검토·문서 관리·사용자 관리)은 **관리자만**, 질문하기는 **로그인한 본인 권한**으로만
동작하도록 SSO 로그인을 붙였습니다. Streamlit 내장 OIDC 로그인을 사용합니다.

> 설정하기 전(=SSO 미설정)에는 '개발 모드'로 로그인 없이 전체 사용 가능합니다.
> 운영에서는 반드시 아래를 설정하세요.

## 1. IT/보안팀(SSO 관리자)에게 요청할 것

이 앱을 사내 SSO에 **OIDC 클라이언트로 등록**해달라고 요청하고, 다음을 받으세요:

- **client_id**, **client_secret**
- **디스커버리 URL**: `https://<사내SSO>/.well-known/openid-configuration`
- 등록 시 **redirect URI** 를 알려줘야 합니다: `http://<앱주소>:8501/oauth2callback`
  (예: `http://hr-rag.company.com:8501/oauth2callback`)

## 2. secrets 파일 작성

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# 편집: client_id/secret, server_metadata_url, redirect_uri, cookie_secret 채우기
python -c "import secrets; print(secrets.token_hex(32))"   # cookie_secret 생성용
```

`.streamlit/secrets.toml` 은 **비밀 정보라 git에 올리지 않습니다**(.gitignore 처리됨).

## 3. 관리자 지정

`config/system.yaml` 의 `permissions.admins` 에 **관리자 이메일**(SSO 로그인 이메일)을 넣습니다:

```yaml
permissions:
  admins:
    - hong@company.com
    - kim@company.com
```

- 여기 있는 이메일만 **문서 관리·사용자 관리·문서 검토** 에 접근합니다.
- 목록이 비어 있고 SSO도 미설정이면 개발 모드(전체 허용)입니다.

## 4. 질문하기 = 본인 계정

로그인하면 **로그인 이메일이 곧 사용자 ID**가 되어, 그 사람의 직책·직무 권한으로만 검색합니다.
따라서 사용자 관리에서 **user_id 를 SSO 이메일과 동일하게** 등록하세요
(예: 사용자 ID `hong@company.com`, 직책 부장, 직무 급여).

## 5. 실행

```bash
pip install -e ".[ingest,ui,postgres]"
streamlit run app/review/streamlit_app.py
```

접속하면 로그인 화면 → SSO 인증 → 관리자면 모든 화면, 일반 사용자면 질문하기만 사용됩니다.

## 요약

| 화면 | 접근 권한 |
|---|---|
| 문서 검토 / 문서 관리 / 사용자 관리 | 관리자(`permissions.admins`)만 |
| 질문하기 | 로그인한 모든 사용자(본인 권한으로 검색) |

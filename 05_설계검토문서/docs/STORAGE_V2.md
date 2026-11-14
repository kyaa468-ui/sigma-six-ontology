# SION 저장 형식 v2

이 문서는 수정계획 01의 구현 계약이다. 기준은 구현 직전의 로컬 소스와 미커밋 변경이다. 기준 사본·파일별 SHA-256·기존 시험 결과는 `agent/work/storage-v2-baseline/`에 보관한다. 기존 리허설 1,176개 파일(481,827,983바이트)은 변경하지 않고 `agent/work/storage-v2-validation/original-rehearsals-copy/`로 복사했다.

## 저장과 호환 읽기

- `snapshot_store.encode/decode`의 v1 포맷과 독립 사본 계약은 유지한다. `common.read_json`은 참조를 해석하지 않는다.
- `storage_objects`의 `$sigma_object_ref.version=2`는 내부 저장 계층에서만 해석한다. 사용자 자료의 같은 모양 객체는 literal로 저장된다. JSON 객체의 원래 키 순서는 보존하고 논리 해시에서만 키를 정렬한다.
- 큰 문자열과 배열을 chunk로 나눈다. `ObjectDict/ObjectList`는 필요한 가지를 읽고 변경한 경로만 독립적으로 보유한다. `deepcopy`가 원본 전체를 펼치지 않는다. 요청의 decoded cache 상한은 64MiB다.
- `record_store.publish`는 블롭 검증 → 작은 `sigma-record@v2` manifest → `.json.commit` → 파생 SQLite 인덱스 순서로 발행한다. manifest에는 논리 SHA-256/바이트 수, root 참조, 소형 검색 필드가 있다. commit에는 별도의 물리 manifest SHA-256/바이트 수가 있다.
- 같은 ID와 다른 논리 내용은 거절한다. 중단된 발행은 조회에서 불완전으로 드러난다. 같은 내용의 명시적 writer 재시도만 발행을 마칠 수 있다. 읽기에서는 복구나 인덱스 생성이 일어나지 않는다.
- 인덱스는 권한이나 원본의 대체물이 아니다. manifest/commit과 대조하고, 누락·손상은 원본 manifest 순회로 보완한다. 원래 도메인의 실행 범위·연구 노출·holdout 검사가 먼저 적용된다.
- 주문 dispatch/identity와 포지션 구간 SQLite는 원래 `work/ontology` 위치를 유지한다. 세대 pointer가 이 원장을 옮기지 않는다. 계좌·잔고·가격의 mutable 파일 계약도 유지한다.

## 명시적 입력 API

`common.load_briefing(view='engine'|'evidence')`, `iter_prep_rows()`, `load_analysis_row(code)`를 사용한다. `write_briefing`과 `write_prep_rows`는 immutable revision을 먼저 발행한 후 최신 pointer를 원자적으로 바꾼다. 파일의 논리 역할과 후보 순서는 유지한다.

실제 읽은 입력은 그 순간 동결한다. 중첩 감사에는 같은 `read_id`를 연결한다. 로그는 파일의 당시 prefix만 읽으며, 기록할 때 JSON 값을 incremental 파싱해 불변 참조로 캡처한다. 사건 ID·관측 시각·결과는 같은 payload라도 각각 유지한다.

## 조회 API

일반 JSON 페이지의 목표 상한은 1MiB, 원문 chunk는 256KiB다. 기본 페이지는 50행, 최대 100행이고 기존 20행 등의 작은 상한은 유지한다. `schema_version=2`, `total`, `next_cursor`, `complete`로 현재 페이지와 전체 범위를 구분한다. 큰 항목의 `detail_required`는 전체 원문을 반환했다는 뜻이 아니다.

| 경로/도구 | 반환 내용 | 후속 조회 |
|---|---|---|
| `/api/view` | 동결 화면의 metadata와 counts | 목록·선택 상세 endpoint |
| `/api/targets`, `/api/cases`, `/api/proposals`, `/api/validations`, `/api/policies`, `/api/reviews` | cursor 목록 | 해당 ID 상세 |
| `/api/trace/<id>`, `/api/case/<id>`, `/api/proposal/<id>`, `/api/validation/<id>`, `/api/policy/<id>` | 선택한 항목의 제한된 상세 | `_detail_links` |
| `/api/chunk/<kind>/<id>?field=...&offset=...` | canonical UTF-8 원문의 base64 조각 | `next_offset`; 마지막 조각만 `complete=true` |
| `get_ai_judgments` | 판단 목록과 coverage 요약 | `get_judgment_detail` |
| `get_judgment_coverage` | 분모·상태·개수 요약 | `get_coverage_detail` |
| `get_execution_trace` | 선택 section 목록 | 같은 도구의 `detail_id`, `field`, `offset` |
| `get_review_cases`, `get_proposal_cases`, `get_validation_cases` | 고정된 사례 목록 | 같은 도구의 `detail_index`, `field`, `byte_offset` |
| `get_close_evaluations` | 평가 회차 목록 | 회차 ID와 `detail=true`, `field`, `offset` |

HTTP 서버의 loopback, Host, Origin, 읽기 전용 메서드와 CSP 검사는 유지한다. 브라우저는 이전 목록·상세를 교체하고 선택하지 않은 상세를 내려받지 않는다. 공개 export는 기존 allowlist에 따라 독립 JSON으로 발행한다. 내부 저장 참조는 공개 데이터 계약에 포함되지 않는다.

선택 상세에서 큰 가지를 생략하더라도 관측값·확신도 보정·주문 수량·평가 판정·검증 상태의 작은 표시값은 유지한다. 이런 가지에는 `preview_only=true`와 원문 연결을 함께 반환하고 전체 응답은 `complete=false`로 표시한다. 원문을 읽지 않았다는 이유로 보관된 결과를 ‘미확인’으로 바꾸지 않는다.

## Sigma 경계

`logger.write_all`은 원본 실행 로그에서 `runtime_summary.json`, `daily_summary.json`을 원자적으로 만든다. 두 파일에는 schema version, 원본 SHA-256/바이트/행수 watermark와 완전성 상태가 있다. 실제 실행 회차와 일별 집계는 별도 구조다. 원시 usage 필드를 보존하고, 시간이 없는 회차를 0초로 확정하지 않는다.

별도 Sigma checkout의 summary 소비 코드·legacy streaming fallback·배포는 이 저장소 밖의 적용 작업이다. 현재 SION에 없는 `shadow.py`, `shadow/compare.py`를 새로 복제하지 않는다.

## 변환·복구

Python 3.12 환경에서 `agent/requirements-storage.lock`을 사용한다. `ijson==3.6.0`이 고정되어 있다.

```sh
.venv/bin/python agent/tools/migrate_storage_v2.py SOURCE
.venv/bin/python agent/tools/migrate_storage_v2.py SOURCE --action convert --destination NEW_GENERATION
.venv/bin/python agent/tools/migrate_storage_v2.py SOURCE --action verify --destination NEW_GENERATION
.venv/bin/python agent/tools/migrate_storage_v2.py RECORD --action export --destination OUTPUT.json
```

기본 동작은 읽기 전용 `scan`이다. 원본 안쪽을 변환 목적지로 지정할 수 없다. checkpoint는 append-only이며 재개 시 원본 hash와 이미 완료한 논리 값을 검사한다. 잘못된 JSONL 행의 offset·길이·원문 hash를 오류 기록에 남기며 정상 완료로 표시하지 않는다. 변환 완료만으로 pointer를 바꾸지 않는다.

세대 전환·복구 시험은 검증한 사본에서만 수행한다. writer가 정지한 상태에서만 명시적 `activate`를 사용한다. 전환 후 새 사건이 생겼다면 `rollback`은 거절하며, 새 기록을 호환 export한 뒤 별도 병합·검증이 필요하다. 자동 삭제, 보관 기간 단축, 블롭 GC는 없다.

## 검증 기록

기존 규칙 시험, 전체 pytest, 고정 입력 오프라인 리허설, 변환·장애·성능 시험을 구분한다. 최종 측정값과 D01~D31의 판정은 `STORAGE_V2_VALIDATION.md`에 기록한다. 시험하지 않은 항목을 완료로 표시하지 않는다.

제안 이력 조회의 `get_learning_proposals`는 기본 `section='proposals'`이며 `submission_history`, `ai_attempts`, `ai_results`를 각각 페이지로 조회한다. `detail_id`로 선택한 항목의 상세를 열고, 큰 필드는 `field`·`offset`으로 이어 읽는다. 제안 실행의 원래 scope 검사를 통과한 뒤에만 반환한다. 연구 store의 `errors(job_id, cursor=..., limit=50)`도 전체 개수와 다음 cursor를 제공하며, 저장 실패 버퍼의 누적 건수·overflow는 별도 `fallback_state`에 남긴다.

로컬 화면의 관계 목록은 `/api/relations/<kind>/<id>/<relation>`에서 조회한다. 지원 관계는 `trace → cases`, `case → proposals`, `proposal → validations`다. 검증 ID에 결과 버전이 연결되어 있으면 `/api/validation/<validation_id>`는 정확한 결과 ID를 고르는 페이지를 반환한다. 선택한 결과의 상세는 `/api/validation/<result_id>`에서 읽는다.

`runtime_summary.operations`는 실제 `prep`·`close` 호출의 시작/종료 사건이다. 원래 source 프로세스의 성공 여부와 오류를 기록하며 AI usage에 합치지 않는다. 일별 집계를 실행 회차로 재구성하지 않는다. 해당 사건의 watermark는 `operation_watermark`로 별도 제공한다.

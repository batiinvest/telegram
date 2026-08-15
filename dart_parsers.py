"""
dart_parsers.py — 공시 카테고리별 파서 파사드 (2026-07 분할)

[구조]
  dart_parse_helpers.py       공용 헬퍼·상수 (_fmt_amount·_f·_clean_* 등)
  dart_parsers_capital.py     자본·주식·배당 파서
  dart_parsers_business.py    계약·실적·재무 파서
  dart_parsers_agm.py         주총·임원·경영·IR 파서
  dart_parsers_holdings.py    지분·대량보유·공개매수·담보 파서
  dart_parsers_status.py      거래정지·신탁·제재·정정 파서
  dart_parsers.py             _PARSER_MAP 등록 + 전 심볼 재수출(하위호환)

새 유형 추가: 해당 카테고리 모듈에 parse_xxx(kv) 작성 후 _PARSER_MAP에 등록.
kv에는 _build_kv 결과 + '_html'(원문)·'_rcept_no'(접수번호) 내부 키가 담긴다.
"""
import re       # noqa: F401  하위호환(기존 dart_parsers.re 참조 보존)
import logging  # noqa: F401

from dart_parse_helpers import *       # noqa: F401,F403  헬퍼·상수 재수출
from dart_parsers_capital import *     # noqa: F401,F403  parse_* 재수출
from dart_parsers_business import *    # noqa: F401,F403
from dart_parsers_agm import *         # noqa: F401,F403
from dart_parsers_holdings import *    # noqa: F401,F403
from dart_parsers_status import *      # noqa: F401,F403


_PARSER_MAP = [
    # 거래정지·권리락은 최우선 — 제목 '(사유)'에 무상증자·유상증자·상장폐지 등이 붙어도
    # 주권매매거래정지/권리락은 항상 그 이벤트 (사유 파서로 새면 빈결과→폴백 노이즈)
    (['거래정지', '매매거래정지'],           parse_trading_halt),
    (['권리락'],                             parse_ex_rights),
    (['유무상증자'],                         parse_combined_ci),
    # 청약결과·발행결과는 '유상증자'보다 먼저 — 제목에 유상증자가 있어 증자결정 파서로 새던 문제
    (['청약결과', '발행결과'],                 parse_subscription_result),
    (['유상증자'],                          parse_rights_offering),
    (['무상증자'],                          parse_bonus_issue),
    (['단일판매', '공급계약체결', '수주'],   parse_contract),
    # 가액조정은 '전환사채'·'신주인수권' 키워드보다 먼저 매칭돼야 함
    (['전환가액의조정', '전환가액조정', '행사가액의조정', '행사가액조정',
      '교환가액의조정', '교환가액조정'],      parse_conversion_adjust),
    # 만기전 사채취득(콜/풋 조기상환)은 발행용 parse_cb보다 먼저 — 제목에 '전환사채'가
    # 있어도 발행 서식이 아니라 취득 서식(한글 번호 키)이라 parse_cb가 빈결과→폴백.
    (['만기전사채취득', '만기전 취득'],       parse_bond_acquisition),
    (['전환사채', '신주인수권부사채'],        parse_cb),
    (['투자판단관련주요경영사항'],           parse_mgmt_event),
    (['기타주요경영사항'],                   parse_misc_mgmt),
    (['임원ㆍ주요주주', '임원·주요주주'],     parse_insider_report),
    # 불성실공시는 상장적격성보다 먼저 — 지정 서식에 '상장적격성 실질심사사유' 필드가
    # 있어 market_measure로 새면 안 됨
    (['불성실공시'],                           parse_unfaithful_disclosure),
    # 관리종목 지정우려(기타시장안내 산문형)는 market_measure보다 먼저 —
    # 제목에 '관리종목'이 있어도 표 없는 산문이라 market_measure가 '(주)'서 문장을
    # 쪼개 깨뜨림. 상장폐지·상장공시위원회 결과 등은 market_measure가 더 잘 처리하므로
    # '기타시장안내' 전체가 아닌 '관리종목지정우려'로만 좁혀 라우팅.
    (['관리종목지정우려'],                     parse_market_notice),
    (['상장폐지', '관리종목', '상장적격성'],   parse_market_measure),
    # 출자법인 회생/파산은 회사 자신의 회생(parse_rehabilitation)보다 먼저 —
    # 제목에 '회생절차'가 있어도 출자법인(투자대상) 서식은 한글 번호키라 별도 처리.
    (['출자법인'],                            parse_investee_rehab),
    # 회생절차(개시신청·개시결정 등) — 국/영문 이중언어라 폴백이 중복 덤프.
    (['회생절차'],                            parse_rehabilitation),
    # 회사합병결정 — 국/영문 이중언어 + 소멸회사 재무제표 덤프.
    (['회사합병'],                            parse_merger),
    (['소송'],                                parse_lawsuit),
    (['횡령', '배임'],                         parse_embezzlement),
    (['배당결정'],                             parse_dividend),
    (['주식소각'],                             parse_share_cancellation),
    (['감자완료'],                             parse_capital_reduction_done),
    (['매출액또는손익구조', '손익구조30'],      parse_earnings_change),
    # 주식담보제공·주식양수도는 최대주주변경보다 먼저 —
    # '최대주주변경을수반하는주식담보제공/양수도…' 제목이 최대주주변경 파서
    # (변경전/후 최대주주 값 없음→빈 결과·오출력)로 새던 문제.
    (['주식담보제공'],                         parse_share_pledge),
    (['주식양수도'],                           parse_share_transfer),
    (['최대주주변경'],                        parse_major_shareholder_change),
    (['주주명부폐쇄', '기준일설정'],           parse_record_date),
    (['전환청구권', '신주인수권', '교환청구권행사'], parse_rights_exercise),
    (['채무보증'],                            parse_debt_guarantee),
    (['주주총회소집결의', '주주총회소집공고'], parse_agm_notice),
    (['주식매수선택권'],                       parse_stock_option),
    (['자기주식처분'],                         parse_treasury_disposal),
    (['파생상품거래손실'],                     parse_derivative_loss),
    (['타법인주식', '출자증권취득'],           parse_equity_acquisition),
    # 대규모 내부거래(공정거래법 제26조) — '매출/매입' 2단 매트릭스 표라 폴백이 깨짐
    (['출자계열회사와의상품', '출자계열회사와의용역',
      '동일인등출자계열회사'],                 parse_intragroup_transaction),
    (['금전대여'],                            parse_money_lending),
    (['기업설명회', 'IR개최'],               parse_ir_event),
    (['주주총회결과'],                        parse_agm_result),
    (['대표이사변경', '임원변경'],            parse_executive_change),
    (['본점소재지변경'],                      parse_hq_relocation),
    (['사외이사의선임', '사외이사선임'],       parse_outside_director),
    (['기업가치제고'],                         parse_value_enhancement),
    (['잠정실적', '잠정영업실적', '영업(잠정)실적'], parse_preliminary_earnings),
    (['신탁계약해지결정'],                       parse_trust_termination_decision),
    (['신탁계약해지결과'],                       parse_trust_termination),
    (['자기주식취득신탁', '자기주식취득결정'],   parse_treasury_acquisition),
    (['대량보유상황보고서'],                      parse_large_holding_report),
    (['공개매수신고서', '공개매수설명서', '공개매수공고'], parse_tender_offer),
    (['공개매수결과보고서', '공개매수청약'],       parse_tender_offer_result),
    (['의견표명서', '공개매수에관한의견'],          parse_tender_opinion),
    # (['주식담보제공'], …) 는 위 최대주주변경 앞으로 이동함
]


_SKIP_DETAIL_TYPES = frozenset([
    '대규모기업집단현황', '기업지배구조보고서',
    # 정기보고서류 — 수천 개 KV + 인코딩 깨짐, 헤더만 표시
    '사업보고서', '반기보고서', '분기보고서', '감사보고서',
    # 지분변동 상세 신고서 — 담당자·tel·헤더셀 노이즈 위주, 제목만 표시
    '소유주식변동신고서',
])

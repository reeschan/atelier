# 設計案「設計の受け渡し」(revision 7) と今の atelier の差

- 設計案: `atelier-handoff.html` (Answer me with HTML のページ。参照した main は `df1925e`)
- 突き合わせた先: ブランチ `claude/intelligent-ritchie-xmydhe` (`a536bbd`、v6 の作り。`~/.claude/skills/atelier` の v7.2 と同じ中身)
- 調べた日: 2026-11-11

区分は 3 つ。

- **入っている**: 今のコードに同じものがある。
- **入っていない**: 今のコードに無い。取り入れる候補。
- **食い違う**: 今のコードに別の決まりがあり、設計案どおりにするなら今の決まりを変える。

v6 では設計者は出番 (episode) ごとに起こされ、ユーザーに直接聞かない。設計案の「聞く」は、出番を kind `decision` の問いで終えること (inline のときだけ AskUserQuestion) と読み替える。

## 1. 受け渡しの中心 (旧 PBI-012〜014)

PR #36 で main に入ったもの。すべて **入っている**。

| 設計案の項目 | 区分 | 今のコードの場所 |
|---|---|---|
| 正本 `docs/pbi/design-record.json` (schema `atelier.design-record/1`、id・revision・targets・concerns だけ) | 入っている | `design_record.py` `write`・`valid_record` |
| 関心事の抜粋 `docs/pbi/notes/<id>.slice.json` (問い・read の path と行・unknown) | 入っている | `design_record.py` `slice` |
| `reply-meta-<name>` を Markdown に付ける (名前・値の形、件数) | 入っている | `design_record.py` `markdown`・`check_meta` |
| 返信の照合: `\r\n` → `\n`、行頭の最後の `am-reply-meta` フェンスだけ読む、`  > ` の引用は読まない | 入っている | `design_record.py` `normalize`・`split_reply` |
| 未選択の ask は未回答で、承認ではない | 入っている | `design_record.py` (終了コード 5)、`conductor.py` `UNANSWERED` |
| 値の不一致・古い revision で止める。フェンスの無い返信は版に結ばない | 入っている | `design_record.py` (終了コード 3・4)、`SKILL.md` `## Questions` |
| `confirm` は工程でなく Approve の見せ方 (auto・off・スキル名) | 入っている | `agents.py` `ADAPTER_PHASES`、`adapter_gate.py` `confirm_row`、`appendix/adapters.md` `## Confirm` |
| 一覧にスキルが無いとき CLI を作らない・呼ばない (G) | 入っている | `appendix/adapters.md` `## Confirm` 3 (「skill の CLI を自分で走らせない・書かない」) |
| 未導入・拒否のときは同じ Markdown をチャットに出す (F) | 入っている | `tools_check.py` `FALLBACK`、`appendix/adapters.md` |
| 導入案内 (repo `answer-me-with-any` の `8856dde51ff8` の `skills/answer-me-with-html`)、yes の前に入れない | 入っている | `tools_check.py` `INSTALL`・`CONFIRM_COMMIT`、`designer.md` `## Approve` |
| `SKILL.md` の Approve でスキルかチャットかを選ぶ | 入っている | `SKILL.md` `## Questions` の `approve` (v6 では呼び出し側の仕事) |
| `appendix/kiln.md` は触らない | 入っている | 触られていない |

## 2. 「確定」の表

| 記号 | 設計案 | 区分 | 今のコード | 取り入れるなら |
|---|---|---|---|---|
| 調査 | 動作に影響する変更は原則調べる (design を走らせる)。省くのは文言・コメント・整形だけ | 食い違う | `appendix/design.md` `## Design: the decision`: 「明らかに単純」(1 モジュール・PBI 3 個以下・新しい概念なし、または方向が決まっている) なら聞かずに skip。間なら `decision` で聞く | skip の条件を「文言・コメント・整形だけの変更」に狭め、それ以外は run にする。「間なら聞く」は残すか消すかを決める |
| 不明 | 設計者は該当する原文だけを読む。解けなければ利用者に聞く | 一部入っている | `design_record.py slice` が read の行と unknown を残す。ただし設計者は不明点を「Open assumptions」に仮定として置き、Approve で直してもらう (`appendix/design.md` `## Design: run` 3) | 解けない不明点は仮定にせず `decision` で聞く、と `appendix/design.md` に書く。読むのは抜粋の行だけ、を明記する |
| モデル | 使えなければ止めて代替を聞く。格上げも引取もしない | 食い違う | `runners.md` `## Common rules`: 使えない runner は `unavailable` を記録して route の次の段へ自動で進む。段上げ・自分で作るのは rework の上限のあと利用者が選んだときだけ (`appendix/rework.md`) | route の段を全部使い切ったら止めて `decision` で代替を聞く、なら今の作りに足すだけで済む。「1 段目が使えなければ即止める」なら route の自動の後ろ盾をやめる大きな変更 |
| OMM | 無ければ作成を聞く。断ったらコードと依存で続ける | 食い違う | `appendix/map.md`: キャッシュはリポジトリ外 (`omm_cache.py`)。miss で `omm`・`omm-scan` があれば聞かずに scan。無ければ代わりの依頼文。道具が無いことは Approve で選ばせる | 「未決」と同じ問い (下の 4) |
| 診断 | 計画時とレビュー後に必要かを判定する。診断の前に聞く | 一部入っている | lint-survey (計画時、design が run のとき) と lint-recheck (verify のあと) がある。model への送信の前は毎回 `send-approval` で聞く。AST だけの実行は聞かない。recheck は必要性を判定せず必ず走る | recheck の前に必要性の判定 (変更が文言だけなら skip など) を足す。「聞く」を AST だけの実行にも広げるかを決める |
| 修正 | 診断後のコード修正は別に承認を取る | 食い違う | survey の `bug-hunt` で範囲内の本物は spec に入り、PBI 一覧の承認でまとめて承認される。recheck の `rework-candidate` は聞かずに rework (`appendix/lint.md` `## Lint recheck` 4) | recheck の rework の前に `decision` で聞く。survey の分は Approve の一覧で別の項目として選ばせる |
| 比較 | 従来と新方式の測り方だけ書く。実行しない | 入っていない | 該当なし (設計案自身の評価の話) | 取り入れるなら `docs/` に測り方のメモを置くだけ。コードの変更は無い |
| 役割 | 値は実行時 → リポジトリ → 共通の順。値と出所をログする | 入っていない | `conductor.py` `load_roles`: home の `agents.json` `roles` と `DEFAULT_ROLES` だけ。実行時の指定 (`start` の引数) もリポジトリの設定 (`docs/pbi/…`) も無く、出所は出さない。不正値は `warn roles.…` で既定に戻す | `start --role designer=opus` のような実行時の指定と、リポジトリの `docs/pbi/roles.json` (仮) を足し、`run.json` と `launch:` の近くに `role designer: opus (from repo)` のように出所を残す |
| F | 未導入・拒否なら同じ Markdown をチャットへ | 入っている | 1 の表 | — |
| G | 一覧にあるときだけ skill の手順 | 入っている | 1 の表 | — |

## 3. そのほかの項目

| 設計案の項目 | 区分 | 今のコード |
|---|---|---|
| `context_prep: false` は明示した移行にだけ使い、失敗の逃げ道にしない | 入っていない | `context_prep` という設定は今のコードにも git の履歴にも無い。取り入れるなら、まず何を指す設定か (文脈の下ごしらえの段?) を決める必要がある |
| security-audit は本件で走らせない | 対象外 | 設計案を作った run だけの決まり。スキルの規則ではない |
| 「食い違い」の節 (versions/ と ACTIVE が無い、停止点 5 つ、OMM はリポジトリ外、home の旧名) | 入っている | 設計案が main をそのまま書き写したもの。v6 では停止点は `designer.md` `## When to stop` の 4 つ + 問いの kind (`approve`・`rework-limit`・`destructive`・`send-approval`・`decision`) |
| 「環境」の節 (クラウドに Agent・python が無い) | 対象外 | 設計案を作った環境の話 |

## 4. 未決: OMM の置き場所

設計案は 2 つの案を並べ、どちらとも決めていない。

| 案 | 中身 | 今のコードとの差 |
|---|---|---|
| A. リポジトリ外のキャッシュ (今の main) | `omm_cache.py` が `.omm-cache/<clone>/<fingerprint>/` に置く。`.omm/` はコミットしない。miss で道具があれば聞かずに scan | 変更なし |
| B. 作成を聞いて、断ったらコードで続ける (以前の grill) | miss のとき scan の前に `decision` で「作るか」を聞く。断ったら代わりの依頼文 (コードと依存から書く) | `appendix/map.md` の Run 3 の前に問いを足す。置き場所そのものは A のまま (リポジトリ外) にもできる |

B は「置き場所」というより「作る前に聞くか」の違いなので、A の置き場所のまま B の「聞く」だけを足す組み合わせもある。

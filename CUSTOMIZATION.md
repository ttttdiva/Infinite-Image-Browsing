# Public customization / upstream policy

このリポジトリは `zanllp/infinite-image-browsing` を基にした公開カスタマイズ版です。
公開先は `ttttdiva/Infinite-Image-Browsing`、通常のブランチは `main` です。
MIT ライセンスと元作者の著作権表示を保持しています。

## 起動とローカルデータ

`run.bat` または `python scripts/image-library.py` で単独起動できます。
`.venv/`、`.local/`、`.env`、画像、DB、キャッシュは各利用者のマシンに置き、Git に追加しないでください。
通常の閲覧に外部 AI サービスの API キーは不要です。依存の導入は README を参照してください。

## オプションの Generation Studio 連携

単独利用時に Generation Studio は不要です。既存の連携との互換性を保つため、launcher は sibling の `73_ComfyUI-Workbench/.local/generation-studio/config.json` を確認します。
設定が存在し、同じ IIB ルート、loopback アドレス、認証済みインスタンスと確認できた場合に限り再利用します。設定がなければ単独起動します。
資格情報を含む設定ファイルは同梱しません。IIB は外部アプリのアセット管理やバックアップを置き換えません。

## 上流更新

本家: https://github.com/zanllp/infinite-image-browsing

この公開版は監査済みソースを新しい初期コミットとして公開するため、本家と Git の履歴がつながっていません。
既存の `scripts/update-upstream.ps1` は共通履歴のある checkout 向けです。この公開版で初回からそのまま実行せず、本家との差分を確認して必要な変更を取り込んでください。
秘密情報やローカル設定を持ち込まないよう、上流更新の都度確認してください。

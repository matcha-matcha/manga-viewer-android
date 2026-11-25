まんがビューワー Androidアプリ(ソース)
=====================================
機能: 初回にLAN内のサーバーを自動探索 → 一覧からタップ(1台だけなら自動) → 記憶 / 全画面 /
      IP変更・PC入れ替え時は同じサーバーを自動で探し直す / 設定画面から接続先の変更も可能

ビルド方法(Android Studio):
  1. Android Studio で「Open」→ この android フォルダを選ぶ
  2. Build > Build APK(s) → app/build/outputs/apk/debug/app-debug.apk をスマホに転送してインストール
     (スマホ側で「提供元不明のアプリ」を許可)

ビルド方法(GitHub Actions / 無料・Android Studio不要):
  1. GitHubで新しいリポジトリを作り、この android フォルダの中身をアップロード
  2. github-actions-build.yml を、リポジトリ内の .github/workflows/build.yml という名前で置く(GitHubの画面で「Add file」→ パスに入力)
  3. 「Actions」タブ → Build APK → Run workflow(または push で自動実行)
  4. 完了後、Artifacts の MangaViewer-apk をダウンロード → 解凍して app-debug.apk をスマホへ

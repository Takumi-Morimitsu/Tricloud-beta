import React from "react";

export default function StripeConnect() {
  return (
    <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
      <h2 className="text-xl font-semibold text-slate-900">報酬の受け取り設定（テスト版）</h2>
      <p className="mt-2 text-sm text-slate-600">
        テストモデルではStripe Connectの実アカウント登録画面は接続しません。
        バックエンドの /billing/stripe/connect/* API と環境変数を設定すると、本番前検証に進めます。
      </p>
    </div>
  );
}

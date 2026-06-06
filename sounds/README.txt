把你的音效檔放這裡（檔名必須一致）：

    403.wav        ← 撞 403 時播（被 server 擋）
    checkout.wav   ← 進結帳頁時播（搶到票）

格式：WAV（PCM）。MP3 不支援（用 Audacity 之類匯出成 wav 即可）。
不放或檔名不對 → 靜默不播，bot 照常運作。

撞 403 警報內建 3 秒 debounce，連續 403 不會狂響。

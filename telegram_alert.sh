TG_TOKEN="8743953981:AAGlIFtMa9YBLxB-DN9S_3LqNHyworqehIc"
TG_CHAT_ID="6847971073"

curl -s -X POST "https://api.telegram.org/bot$TG_TOKEN/sendMessage" \
  -d "chat_id=$TG_CHAT_ID" \
  -d "text=測試通知！"

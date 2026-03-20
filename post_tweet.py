import tweepy
import base64
import json
import os
from datetime import datetime
from googleapiclient.discovery import build
from google.oauth2 import service_account
import google.generativeai as genai

# --- 設定 ---
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
DRIVE_POSTED_FOLDER_ID = os.environ["DRIVE_POSTED_FOLDER_ID"]

# --- Google Drive クライアント ---
def get_drive_client():
    creds_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

# --- Google Driveから写真を1枚取得 ---
def download_next_photo(drive) -> tuple[str, str] | None:
    results = drive.files().list(
        q=f"'{DRIVE_FOLDER_ID}' in parents and mimeType contains 'image/' and trashed=false",
        fields="files(id, name)",
        pageSize=1,
        orderBy="createdTime"
    ).execute()

    files = results.get("files", [])
    if not files:
        print("投稿できる写真がありません")
        return None

    file = files[0]
    file_id = file["id"]
    file_name = file["name"]

    # ダウンロード
    content = drive.files().get_media(fileId=file_id).execute()
    local_path = f"/tmp/{file_name}"
    with open(local_path, "wb") as f:
        f.write(content)

    print(f"ダウンロード完了: {file_name}")
    return local_path, file_id

# --- 投稿済みフォルダに移動 ---
def move_to_posted(drive, file_id: str):
    drive.files().update(
        fileId=file_id,
        addParents=DRIVE_POSTED_FOLDER_ID,
        removeParents=DRIVE_FOLDER_ID
    ).execute()
    print("投稿済みフォルダに移動しました")

# --- Geminiで文章生成 ---
def generate_tweet(image_path: str) -> str:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-1.5-flash")

    ext = image_path.split(".")[-1].lower()
    mime_type = "image/png" if ext == "png" else "image/jpeg"

    with open(image_path, "rb") as f:
        image_data = f.read()

    response = model.generate_content([
        {
            "mime_type": mime_type,
            "data": image_data
        },
        """この犬の写真を見て、Xでバズりやすいツイート文章を日本語で1つ作成してください。
条件：
- 140文字以内
- 絵文字を適度に使う
- 犬好きの心をつかむ表現
- ハッシュタグを2〜3個（#犬 #いぬのいる生活 など）
- 文章のみ返答してください"""
    ])
    return response.text

# --- Xに投稿 ---
def post_to_x(image_path: str, text: str):
    auth = tweepy.OAuth1UserHandler(
        os.environ["X_API_KEY"],
        os.environ["X_API_SECRET"],
        os.environ["X_ACCESS_TOKEN"],
        os.environ["X_ACCESS_TOKEN_SECRET"]
    )
    api = tweepy.API(auth)
    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"]
    )

    media = api.media_upload(filename=image_path)
    client.create_tweet(text=text, media_ids=[media.media_id])
    print("X投稿完了！")

# --- ログ更新 ---
def update_log(file_name: str):
    log_path = "posted_log.json"
    with open(log_path) as f:
        log = json.load(f)

    log["posted"].append(file_name)
    log["history"].append({
        "file": file_name,
        "posted_at": datetime.utcnow().isoformat()
    })

    with open(log_path, "w") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)
    print("ログ更新完了")

# --- メイン ---
def main():
    drive = get_drive_client()

    result = download_next_photo(drive)
    if not result:
        return

    local_path, file_id = result
    file_name = os.path.basename(local_path)

    text = generate_tweet(local_path)
    print(f"生成された文章:\n{text}")

    post_to_x(local_path, text)
    move_to_posted(drive, file_id)
    update_log(file_name)

if __name__ == "__main__":
    main()

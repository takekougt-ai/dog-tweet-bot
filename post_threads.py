import json
import os
import time
import subprocess
import shutil
import requests
from datetime import datetime
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google import genai
from google.genai import types

DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
DRIVE_POSTED_THREADS_FOLDER_ID = os.environ["DRIVE_POSTED_THREADS_FOLDER_ID"]
DRIVE_POSTED_X_FOLDER_ID = os.environ["DRIVE_POSTED_X_FOLDER_ID"]
THREADS_ACCESS_TOKEN = os.environ["THREADS_ACCESS_TOKEN"]
GITHUB_REPOSITORY = os.environ["GITHUB_REPOSITORY"]

PROMPT = """この犬の写真を見て、投稿する文章を日本語で1つ書いてください。

以下のルールを厳守してください：
- 絵文字は一切使わない
- 感嘆符（！）や過剰な句読点を避ける
- 「かわいい」「癒される」などの陳腐な表現は使わない
- おしゃれでシュールなトーンで書く
- 犬を擬人化したり、哲学的・文学的な視点で描写してもよい
- ハッシュタグを除いた本文は3行以内・100文字以内
- 文章のみ返答してください"""


def get_creds():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    creds.refresh(Request())
    return creds


def get_drive_client(creds):
    return build("drive", "v3", credentials=creds)


def download_next_photo(drive):
    results = drive.files().list(
        q="'" + DRIVE_FOLDER_ID + "' in parents and mimeType contains 'image/' and trashed=false",
        fields="files(id, name)", pageSize=1, orderBy="createdTime"
    ).execute()

    files = results.get("files", [])
    if not files:
        print("投稿できる写真がありません")
        return None

    file = files[0]
    content = drive.files().get_media(fileId=file["id"]).execute()
    local_path = "/tmp/" + file["name"]
    with open(local_path, "wb") as f:
        f.write(content)

    print("ダウンロード完了: " + file["name"])
    return local_path, file["id"]


def convert_to_jpeg(local_path):
    if not local_path.lower().endswith(".heic"):
        return local_path

    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()

    jpeg_path = local_path.rsplit(".", 1)[0] + ".jpg"
    img = Image.open(local_path)
    img.save(jpeg_path, "JPEG")
    print("HEIC→JPEG変換完了: " + jpeg_path)
    return jpeg_path


def upload_to_folder(local_path, folder_id, creds):
    file_name = os.path.basename(local_path)
    ext = file_name.split(".")[-1].lower()
    mime_type = "image/png" if ext == "png" else "image/jpeg"

    token = creds.token
    metadata = json.dumps({"name": file_name, "parents": [folder_id]}).encode("utf-8")

    with open(local_path, "rb") as f:
        image_data = f.read()

    boundary = "boundary_abc123"
    body = (
        "--" + boundary + "\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        + metadata.decode("utf-8")
        + "\r\n--" + boundary + "\r\n"
        "Content-Type: " + mime_type + "\r\n\r\n"
    ).encode("utf-8") + image_data + ("\r\n--" + boundary + "--").encode("utf-8")

    res = requests.post(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "multipart/related; boundary=" + boundary,
        },
        data=body
    )
    print("Driveアップロード: " + str(res.status_code))
    res.raise_for_status()
    print("posted_threadsフォルダにアップロードしました")


def delete_if_both_posted(drive, file_id, local_path):
    file_name = os.path.basename(local_path)

    results = drive.files().list(
        q="'" + DRIVE_POSTED_X_FOLDER_ID + "' in parents and name='" + file_name + "' and trashed=false",
        fields="files(id)"
    ).execute()

    if results.get("files"):
        drive.files().delete(fileId=file_id).execute()
        print("両方投稿済みのため元ファイルを削除しました")
    else:
        print("X側未投稿のため元ファイルは保持します")


def push_image_and_get_url(local_path):
    file_name = os.path.basename(local_path)
    dest_path = "posted_images/" + file_name

    os.makedirs("posted_images", exist_ok=True)
    shutil.copy(local_path, dest_path)

    subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
    subprocess.run(["git", "config", "user.email", "actions@github.com"], check=True)
    subprocess.run(["git", "add", dest_path], check=True)
    subprocess.run(["git", "commit", "-m", "Add image for posting: " + file_name], check=False)
    subprocess.run(["git", "push"], check=True)

    raw_url = "https://raw.githubusercontent.com/" + GITHUB_REPOSITORY + "/main/" + dest_path
    print("GitHub raw URL: " + raw_url)
    time.sleep(5)
    return raw_url


def generate_caption(image_path):
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    ext = image_path.split(".")[-1].lower()
    mime_type = "image/png" if ext == "png" else "image/jpeg"

    with open(image_path, "rb") as f:
        image_data = f.read()

    response = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=[types.Part.from_bytes(data=image_data, mime_type=mime_type), PROMPT]
    )
    return response.text.strip()


def post_to_threads(image_url, caption):
    token = THREADS_ACCESS_TOKEN

    me_res = requests.get(
        "https://graph.threads.net/v1.0/me",
        params={"fields": "id", "access_token": token}
    )
    me_res.raise_for_status()
    user_id = me_res.json()["id"]

    container_res = requests.post(
        "https://graph.threads.net/v1.0/" + user_id + "/threads",
        params={"access_token": token},
        json={"media_type": "IMAGE", "image_url": image_url, "text": caption}
    )
    print("コンテナレスポンス: " + str(container_res.status_code))
    container_res.raise_for_status()
    container_id = container_res.json()["id"]

    time.sleep(10)

    publish_res = requests.post(
        "https://graph.threads.net/v1.0/" + user_id + "/threads_publish",
        params={"access_token": token},
        json={"creation_id": container_id}
    )
    print("公開レスポンス: " + str(publish_res.status_code))
    publish_res.raise_for_status()
    print("Threads投稿完了！ ID: " + publish_res.json()["id"])


def update_log(file_name):
    log_path = "posted_log.json"
    with open(log_path) as f:
        log = json.load(f)
    log["posted"].append(file_name)
    log["history"].append({"file": file_name, "posted_at": datetime.utcnow().isoformat(), "platform": "threads"})
    with open(log_path, "w") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)
    print("ログ更新完了")


def main():
    creds = get_creds()
    drive = get_drive_client(creds)

    result = download_next_photo(drive)
    if not result:
        return

    local_path, file_id = result
    local_path = convert_to_jpeg(local_path)
    file_name = os.path.basename(local_path)

    image_url = push_image_and_get_url(local_path)
    caption = generate_caption(local_path)
    print("生成された文章:\n" + caption)

    post_to_threads(image_url, caption)
    upload_to_folder(local_path, DRIVE_POSTED_THREADS_FOLDER_ID, creds)
    delete_if_both_posted(drive, file_id, local_path)
    update_log(file_name)

    subprocess.run(["git", "add", "posted_log.json"], check=True)
    subprocess.run(["git", "commit", "-m", "Update posted log [threads]"], check=False)
    subprocess.run(["git", "push"], check=True)


if __name__ == "__main__":
    main()

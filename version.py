"""版本管理與更新檢查。

HTTP 入口 `python main.py` 啟動時不會自動執行；需要時請跑 `python version.py`。
"""
import os
import sys
import subprocess
import requests

CURRENT_VERSION = "1.0.0"
GITHUB_OWNER = "paulkuo123"
GITHUB_REPO = "InventoryCalculator"


def check_for_updates():
    """開發者環境檢查 Git 遠端；打包執行檔檢查 GitHub Releases。"""
    print("\n" + "=" * 50)
    print(f"📦 目前版本：v{CURRENT_VERSION}")
    print("=" * 50)

    is_developer = os.path.exists(".git") and not getattr(sys, "frozen", False)
    if is_developer:
        _check_git_updates()
    else:
        _check_github_releases()


def _check_git_updates():
    """開發者模式：fetch 後比對 HEAD 與遠端分支。"""
    try:
        subprocess.run(
            ["git", "fetch", "--quiet"],
            capture_output=True,
            timeout=10,
        )

        local = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()

        remote = None
        for branch in ("origin/main", "origin/master", "origin/develop"):
            result = subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                remote = result.stdout.strip()
                break

        if remote is None:
            print("⚠️  無法找到遠端分支，跳過更新檢查")
            return

        if local != remote:
            log = subprocess.run(
                ["git", "log", remote, "-1", "--oneline"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            print("🔄 發現程式碼更新！")
            print(f"   遠端最新：{log}")
            print("   請執行：git pull 更新原始碼")
        else:
            print("✅ 程式碼已是最新版本")

    except subprocess.TimeoutExpired:
        print("⚠️  Git 檢查超時，跳過更新檢查")
    except FileNotFoundError:
        print("⚠️  未安裝 Git，跳過更新檢查")
    except Exception as e:
        print(f"⚠️  Git 檢查失敗：{e}")


def _check_github_releases():
    """一般使用者模式：比對 GitHub Releases 最新 tag。"""
    api_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
    try:
        response = requests.get(api_url, timeout=10)
        if response.status_code == 404:
            print("⚠️  尚未發布 GitHub Release，跳過版本比對")
            return
        if response.status_code != 200:
            print(f"⚠️  無法取得 GitHub Releases 資訊（HTTP {response.status_code}）")
            return

        data = response.json()
        latest_version = data.get("tag_name", "").lstrip("v")
        release_url = data.get("html_url", "")
        if not latest_version:
            print("⚠️  尚未發布任何 Release")
            return

        if latest_version != CURRENT_VERSION:
            print(f"🔄 發現新版本：v{latest_version}（目前是 v{CURRENT_VERSION}）")
            if release_url:
                print("   請到以下網址下載執行檔：")
                print(f"   {release_url}")
        else:
            print("✅ 已是最新版本")

    except requests.exceptions.Timeout:
        print("⚠️  GitHub API 連線超時")
    except requests.exceptions.RequestException as e:
        print(f"⚠️  GitHub API 請求失敗：{e}")
    except Exception as e:
        print(f"⚠️  檢查更新失敗：{e}")


if __name__ == "__main__":
    check_for_updates()

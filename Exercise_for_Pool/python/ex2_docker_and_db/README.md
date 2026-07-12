# 課題2 Docker & DB

課題2-1、課題2-2用の Docker 環境と提出用ソースコードです。

## 起動

```powershell
docker compose up -d --build
```

## 課題2-1の確認

```powershell
docker compose exec app bash
cat /etc/os-release
python3.8 --version
mysql --default-character-set=utf8mb4 -hmysql -uex2_user -pex2_password -e "select version();"
```

上記の出力が映るようにスクリーンショットを撮り、`ex2-1.png` として提出します。

## 課題2-2の実行

```powershell
docker compose exec app python3.8 2-2.py
```

## 課題2-2の確認SQL

```powershell
docker compose exec mysql mysql --default-character-set=utf8mb4 -uex2_user -pex2_password ex2 -e "select count(URL) from ex2_2;"
docker compose exec mysql mysql --default-character-set=utf8mb4 -uex2_user -pex2_password ex2 -e "show columns from ex2_2;"
docker compose exec mysql mysql --default-character-set=utf8mb4 -uex2_user -pex2_password ex2 -e "select * from ex2_2 limit 5;"
```

それぞれ `ex2-2_count.png`、`ex2-2_columns.png`、`ex2-2_table.png` として提出します。

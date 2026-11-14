-- MySQL Workbench의 로컬 관리자 연결에서 실행합니다.
-- 아래 비밀번호를 본인 PC에서만 정한 값으로 바꾼 후 실행하세요.
-- 기존 계정이 있으면 비밀번호를 자동 변경하지 않습니다.
CREATE DATABASE IF NOT EXISTS sessionquant
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS 'sessionquant'@'localhost'
  IDENTIFIED BY '여기를_본인만_아는_비밀번호로_바꾸세요';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX
  ON sessionquant.* TO 'sessionquant'@'localhost';

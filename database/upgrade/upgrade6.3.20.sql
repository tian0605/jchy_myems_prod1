-- Back up myems_system_db before upgrading from 6.3.19.
-- Additive and rerunnable; rollback code without dropping conversation evidence.
CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_ai_chat_contexts` (
  `message_id` BIGINT NOT NULL,
  `context_json` LONGTEXT NOT NULL,
  PRIMARY KEY (`message_id`)
);

UPDATE `myems_system_db`.`tbl_versions`
SET version='6.3.20', release_date='2026-10-18'
WHERE id=1 AND version='6.3.19';

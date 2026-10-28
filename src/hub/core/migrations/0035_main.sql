-- Люди (Ф21-1, docs/architecture.md и поправки П5, П9). Только главный файл: work остаётся на
-- версии 33 до 0036.
--
-- Человек — группа контактов (`contact.person_id` в файле контакта, связи через границу файлов нет). Строка
-- человека — без ПДн (П5): имя, заданное владельцем, лежит в `contact.person_name` (0036) у всех контактов
-- человека, поэтому удаление рабочего файла стирает и его. Пустые люди убирает проход связывания (B4).
CREATE TABLE IF NOT EXISTS person (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts INTEGER NOT NULL
);
-- «Это разные люди» (владелец нажал «Нет» на предложении или разъединил): склейка по имени/нику их не предлагает,
-- по почте — не склеивает. a < b.
CREATE TABLE IF NOT EXISTS person_not_same (
    a INTEGER NOT NULL,
    b INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (a, b),
    CHECK (a < b)
);

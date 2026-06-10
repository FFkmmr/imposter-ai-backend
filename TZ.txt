1. Продуктовая рамка

Название проектаImposter ai iOS App

Платформа MVPiPhone, портретная ориентация, iOS  17+ 

ПозиционированиеВеселая локальная party game на одном телефоне, где игроки по очереди получают роли и слова, обсуждают, блефуют и голосуют за импостера.

Цель MVPДать быстрый, очень простой и красивый игровой опыт:

без регистрации

без обязательного интернета для базовой игры

с понятным запуском за 30–60 секунд

с сильным UX передачи телефона между игроками

Модель монетизацииFree core + Premium AI/content (токены или инапы)

бесплатно: базовая локальная игра, готовые наборы слов, часть категорий, базовые режимы

premium: AI-темы, premium categories, возможно расширенные Party Mode-роли и future content packs

Этапы

MVP: только локальная игра

Phase 2: online mode, daily categories, premium custom categories, analytics growth layer, social sharing

2. Scope MVP

Обязательный функционал MVP

создание матча

ввод/редактирование списка игроков

выбор количества импостеров

выбор режима слов

показ ролей по очереди на одном устройстве

скрытый reveal через жест

генерация слов:

готовые наборы

AI-тема

импорт своих слов

игровой цикл:

раунд подсказок

таймер

голосование

reveal результата

история раунда

быстрый restart / rematch

onboarding с очень простыми правилами

paywall только на premium-функциях

Party Mode с дополнительными ролями

мультиязычность

Что не входит в MVP

online rooms

аккаунты

друзья / профили

push-уведомления

UGC moderation system beyond simple safeguards

social clip generator

deep analytics experiments

live ops content system beyond basic remote config

3. Scope Phase 2

Online Mode с комнатами по коду/ссылке

daily rotating categories

premium custom categories

retention / conversion analytics

shareable results / short clips / cards for social growth

4. Целевая аудитория

компании друзей

пары / небольшие группы

вечеринки

road trips

студенты

family gatherings

casual users, которым нужна игра “открыл и сразу играешь”

5. Core Game Design

Базовый игровой цикл

Пользователь открывает приложение

Создает матч

Добавляет игроков

Выбирает настройки раунда

Игра распределяет роли и слова

Игроки по очереди получают экран со своей ролью

Начинается фаза подсказок

Идет обсуждение

Все голосуют

Приложение показывает результат

Пользователь может начать следующий раунд

Роли MVP

Civilian

Impostor

Party Mode ролиСостав можно ограничить 2–4 дополнительными ролями, чтобы не перегрузить MVP:

Undercover: получает похожее, но не то же слово

Detective: может получить небольшую подсказку о составе ролей

Joker или Chaos Role: выигрывает при специальном условии

Double Impostor: режим с 2 импостерами

Важно: Party Mode должен ощущаться как расширение, но не ломать базовую простоту.

6. Режимы слов

В MVP

Ready-made categories

AI theme

Import custom words

Детализация

Ready-made: наборы вроде Animals, Food, Movies, Travel, Sports

AI theme: пользователь вводит тему, backend возвращает слово или пару слов под текущий режим

Import: пользователь вставляет свой список слов или пар слов

Типы контента

Single secret word: все мирные знают одно слово, impostor не знает слова

Paired words: разные близкие слова для разных ролей или командных вариаций

Для MVP можно начать с single word и paired party mode

7. UX-поток экранов

MVP экраны

Splash / launch

Home

New Game setup

Players

Game Settings

Word Source / Category Picker

AI Theme Input

Import Words

Pass-the-phone / Hidden role reveal

Role Reveal

Round Timer

Voting

Result

Round History

Rematch / Restart

Onboarding

Paywall

Settings

Language selector

Ключевые UX-принципы

минимум текста

крупные кнопки

очень ясный flow “передай телефон”

сильная приватность при reveal

минимум полей ввода

setup не больше чем в 3–4 шага

8. ТЗ для дизайна

Цель дизайн-командыСделать яркий, запоминающийся, современный mobile-first интерфейс, который поддерживает атмосферу вечеринки, а не выглядит как utility app.

Бренд-направлениеОпора на impostergame.ai, но в app-версии нужно усилить:

playful tension

интригу

социальное взаимодействие

ощущение “секретной роли”

Что нужно подготовить

UI kit

дизайн-система

иконка приложения

набор onboarding-иллюстраций / motion hints

макеты всех MVP-экранов

paywall screens

Party Mode variations

localization-safe layouts

App Store screenshots

Дизайн-системаНужно определить:

цветовую палитру

типографику

кнопки

карточки

модальные окна

bottom sheets

input fields

chips / tags категорий

таймер / progress components

states: loading, empty, error, premium locked

Требования к визуальному стилю

не “techy AI tool”, а “fun social party game”

сильный контраст

акцент на secrecy / reveal / suspense

дружелюбно, но не по-детски

premium должен выглядеть желанно, но не агрессивно

Ключевые дизайн-задачи

продумать лучший role reveal flow

продумать anti-peek UX:

удержание

свайп

shield overlay

“ready for next player” interstitial

сделать Party Mode понятным даже новичкам

сделать onboarding в 3–4 карточках максимум

предусмотреть длинные тексты для локализации

Артефакты на выходе

user flow map

wireframes

final hi-fi screens

clickable prototype

components & tokens

handoff in Figma

App Store creative set

9. ТЗ для iOS-разработки

Технологический стекРекомендуемо:

SwiftUI

MVVM или TCA, если команда его уже использует

StoreKit 2

Firebase Remote Config или аналог

Firebase Analytics / RevenueCat можно подключить уже ближе к Phase 2, но подписки лучше проектировать сразу правильно

локальное хранение: UserDefaults + SwiftData/Core Data при необходимости

Основные модули

App Core

Game Setup

Player Management

Word Content

Role Distribution Engine

Round Flow

Timer & Voting

Results & History

Localization

Monetization

Settings

Analytics hooks

Функциональные требования

локальная игра должна работать без аккаунта

базовые ready-made categories должны быть доступны offline

AI theme требует сеть

импорт слов должен работать локально

если AI недоступен, приложение не должно ломать flow

все игровые состояния должны сохраняться при сворачивании приложения

при accidental app close пользователь должен иметь возможность вернуться в текущий матч

Игровой движокНужно реализовать:

создание GameSession

создание Round

распределение ролей

распределение слова/слов

конфиг количества impostor roles

режимы Party Mode

vote counting

win condition resolution

round history snapshot

Анти-ошибки

защита от старта игры с недопустимым числом игроков

защита от конфигураций, где роли невозможно корректно раздать

защита от пустых custom word imports

graceful fallback, если AI не вернул результат

edge cases:

3 игрока

много игроков

2 импостера

restart same players

change settings mid-session

Non-functional

быстрый cold start

плавные анимации

стабильная работа офлайн

accessibility basics:

Dynamic Type where possible

VoiceOver labels for controls

color contrast

локализация без hardcoded strings

crash-safe recovery

Подписки / paywallВ MVP paywall должен:

появляться только при попытке использовать premium feature

не мешать базовой игре

поддерживать restore purchases

поддерживать feature gating:

AI theme

premium categories

возможно часть Party roles

Минимальный список событий для логированияДаже если полноценная аналитика во 2 фазе, events заложить сразу стоит:

app_open

onboarding_complete

game_create_started

game_started

category_selected

ai_theme_requested

paywall_viewed

subscription_started

round_completed

rematch_started

10. ТЗ для backend

Для local-only MVP backend нужен не для core gameplay, а для контента и premium-логики.

Задачи backend-команды в MVP

API для AI-генерации тем / слов

API для premium content config

удаленная конфигурация доступных категорий

feature flags

subscription entitlement verification при необходимости

basic admin/content panel или простой способ обновлять контент

Что backend не делает в MVP

не хранит обязательный игровой state локальных матчей

не требует регистрации пользователей

не нужен realtime multiplayer

Рекомендуемая архитектура

lightweight API

Postgres или managed DB

server-side integration with LLM

cache layer для типовых AI-запросов

admin table для категорий и контент-паков

Сущности

Category

WordPack

PremiumFlag

LocalizationContent

AITopicRequestLog

FeatureConfig

Эндпоинты MVP

GET /categories

GET /categories/premium

POST /ai/generate-theme

POST /ai/generate-words

GET /config

GET /localizations при необходимости серверной конфигурации

POST /purchase/validate если проверка покупок делается на backend

Требования к AI-генерации

генерировать безопасные, понятные, party-friendly слова

фильтровать NSFW, hate, illegal and risky content

поддерживать много языков

поддерживать короткий ответ и низкую задержку

fallback на curated content, если AI unavailable

AI output contractДля MVP backend должен возвращать структурированный ответ:

язык

тема

список слов или пар слов

difficulty

is_safe

fallback_used

Контент-модерацияНужно:

prompt guardrails

blacklist / moderation layer

запрет токсичных, сексуальных, экстремистских тем

ограничение на длину пользовательского prompt

логирование rejected requests

Локализация на backendЕсли AI работает на нескольких языках:

принимать locale

генерировать слова с учетом языка

не смешивать языки в одном результате

уметь fallback-нуть на английский или локальный curated pack по продуктовой логике

Нефункциональные требования backend

быстрый ответ для AI feature

retry/fallback policy

логирование ошибок

rate limiting

простая observability

защита API key

сервер не должен отдавать прямой доступ к модели из клиента

11. ТЗ для контент-команды / product content

Если у вас есть отдельные люди под контент, им тоже лучше дать отдельный блок.

Нужно подготовить

базовые категории для free tier

premium categories

descriptions для категорий

короткие onboarding тексты

правила игры в ultra-simple wording

party role explanations

тексты paywall

App Store metadata

локализованные строки

Контент-требования

слова должны быть массово понятны

избегать слишком нишевых слов в free packs

premium packs могут быть более тематическими

категории должны подходить для быстрой устной игры

слова не должны быть слишком похожими, если это ломает механику

12. Мультиязычность

Языки MVPРекомендую стартовать с:

English

Russian

Spanish

Portuguese (Brazil)

French

German



Требования

все строки через localization system

AI requests учитывают язык интерфейса или язык, выбранный в игре

категории и onboarding должны быть локализованы

дизайн должен выдерживать длинные переводы

backend должен возвращать контент в выбранной локали

13. QA / тестирование

Что тестировать

создание матча

граничные случаи числа игроков

корректное распределение ролей

скрытый reveal

timer / pause / resume

voting logic

rematch

import words

AI generation success/fail

premium gating

purchase restore

localization regressions

app background / foreground recovery

Типы тестов

unit tests для game logic

UI tests для critical flows

manual QA на реальных устройствах
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Campaign:
    key: str
    title: str
    campaign_id: str
    fixed_params: dict = field(default_factory=dict)
    # В params передаются brand/model: вручную или из ротации model_marka.txt
    car_params: bool = False


CAMPAIGNS: dict[str, Campaign] = {
    "microloans_ru": Campaign(
        key="microloans_ru",
        title="Микрозаймы (RU)",
        campaign_id="9a8b7c6d-5e4f-3120-8b1c-2d3e4f5a6b7c",
        fixed_params={"client_lang": "ru"},
    ),
    "microloans_uz": Campaign(
        key="microloans_uz",
        title="Микрозаймы (UZ)",
        campaign_id="9a8b7c6d-5e4f-3120-8b1c-2d3e4f5a6b7c",
        fixed_params={"client_lang": "uz"},
    ),
    "limits_ru": Campaign(
        key="limits_ru",
        title="Лимиты (RU)",
        campaign_id="4d5e6f7a-8b9c-4d0e-1f2a-3b4c5d6e7f8a",
        fixed_params={"client_lang": "ru"},
    ),
    "limits_uz": Campaign(
        key="limits_uz",
        title="Лимиты (UZ)",
        campaign_id="4d5e6f7a-8b9c-4d0e-1f2a-3b4c5d6e7f8a",
        fixed_params={"client_lang": "uz"},
    ),
    "uzum_auto": Campaign(
        key="uzum_auto",
        title="Узум авто",
        campaign_id="b3c4d5e6-f7a8-4901-9b2c-3d4e5f6a7b8c",
        car_params=True,
    ),
    "credits": Campaign(
        key="credits",
        title="Кредиты",
        campaign_id="dd426938-5558-4e09-9fd1-d039e11e4bca",
    ),
}

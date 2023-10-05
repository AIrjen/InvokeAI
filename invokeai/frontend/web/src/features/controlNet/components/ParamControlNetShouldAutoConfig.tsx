import { useAppDispatch } from 'app/store/storeHooks';
import IAISwitch from 'common/components/IAISwitch';
import { controlAdapterAutoConfigToggled } from 'features/controlNet/store/controlAdaptersSlice';
import { memo, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useControlAdapterIsEnabled } from '../hooks/useControlAdapterIsEnabled';
import { useControlAdapterShouldAutoConfig } from '../hooks/useControlAdapterShouldAutoConfig';

type Props = {
  id: string;
};

const ParamControlNetShouldAutoConfig = ({ id }: Props) => {
  const isEnabled = useControlAdapterIsEnabled(id);
  const shouldAutoConfig = useControlAdapterShouldAutoConfig(id);
  const dispatch = useAppDispatch();
  const { t } = useTranslation();

  const handleShouldAutoConfigChanged = useCallback(() => {
    dispatch(controlAdapterAutoConfigToggled({ id }));
  }, [id, dispatch]);

  return (
    <IAISwitch
      label={t('controlnet.autoConfigure')}
      aria-label={t('controlnet.autoConfigure')}
      isChecked={shouldAutoConfig}
      onChange={handleShouldAutoConfigChanged}
      isDisabled={!isEnabled}
    />
  );
};

export default memo(ParamControlNetShouldAutoConfig);
